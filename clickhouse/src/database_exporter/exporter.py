from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clickhouse import ClickHouseDatabase, SourceTable
from .config import Config
from .storage import AwsCredentials, S3Object, S3Storage


log = logging.getLogger(__name__)
PARAMETERIZED_VIEW = re.compile(r"\{[^{}]+:[^{}]+\}")
UNSUPPORTED_SHARED_ENGINE = re.compile(r"Shared[A-Za-z]*MergeTree")


@dataclass(frozen=True)
class ExportTable:
    name: str
    engine: str
    parameterized: bool


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def json_text(value: Any) -> str:
    return f"{json.dumps(value, indent=2)}\n"


def portable_ddl(ddl: str, database_name: str) -> str:
    result = ddl.replace(f"`{database_name}`.", "").replace(
        f"{database_name}.", ""
    )
    result = re.sub(
        r"SharedMergeTree\('[^']*',\s*'[^']*'\)",
        "MergeTree",
        result,
    )
    result = re.sub(
        r"SharedReplacingMergeTree\('[^']*',\s*'[^']*',\s*([^)]+)\)",
        r"ReplacingMergeTree(\1)",
        result,
    )
    return re.sub(
        r"SharedReplacingMergeTree\('[^']*',\s*'[^']*'\)",
        "ReplacingMergeTree",
        result,
    )


def validate_schema(schema: str, database_name: str) -> None:
    if re.search(r"^ENGINE = ", schema, flags=re.MULTILINE) is None:
        raise RuntimeError("schema.sql does not contain multiline table DDL")
    if f"{database_name}." in schema or f"`{database_name}`." in schema:
        raise RuntimeError("schema.sql contains the source database name")
    if UNSUPPORTED_SHARED_ENGINE.search(schema):
        raise RuntimeError("schema.sql contains an unsupported SharedMergeTree engine")


def clickhouse_inventory(objects: list[S3Object]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in objects:
        parts = item.relative_key.split("/")
        if len(parts) != 2 or not parts[0].startswith("dump_") or not parts[1]:
            continue
        grouped.setdefault(parts[0], []).append(
            {"name": parts[1], "size": item.size}
        )
    return [
        {"dir": directory, "files": sorted(files, key=lambda item: item["name"])}
        for directory, files in sorted(grouped.items(), reverse=True)
    ]


def mysql_inventory(objects: list[S3Object]) -> list[dict[str, Any]]:
    return sorted(
        (
            {"name": item.relative_key, "size": item.size}
            for item in objects
            if item.relative_key and "/" not in item.relative_key
        ),
        key=lambda item: item["name"],
        reverse=True,
    )


class DatabaseExporter:
    def __init__(
        self,
        config: Config,
        clickhouse: ClickHouseDatabase,
        storage: S3Storage,
        credentials: AwsCredentials,
    ) -> None:
        self._config = config
        self._clickhouse = clickhouse
        self._storage = storage
        self._credentials = credentials

    def run(self) -> None:
        self._config.work_dir.mkdir(parents=True, exist_ok=True)
        schema_version = self._clickhouse.schema_version()
        clickhouse_version = self._clickhouse.server_version()
        dump_dir = (
            f"dump_{datetime.now(timezone.utc):%Y_%m_%d}"
            f"_v{schema_version.replace('.', '_')}"
        )
        s3_base = (
            f"https://{self._config.aws_s3_dump_bucket}.s3."
            f"{self._config.aws_s3_region}.amazonaws.com/"
            f"{self._config.aws_s3_dump_prefix}/{dump_dir}"
        )

        log.info("Database: %s", self._config.database_name)
        log.info("Schema version: %s", schema_version)
        log.info("ClickHouse version: %s", clickhouse_version)
        log.info("Dump directory: %s", dump_dir)

        tables, schema = self._generate_schema()
        manifest_tables = self._export_tables(tables, s3_base)
        manifest = {
            "timestamp": utc_timestamp(),
            "clickhouse_version": clickhouse_version,
            "schema_version": schema_version,
            "tables": manifest_tables,
        }
        manifest_content = json_text(manifest)
        self._write_work_file("schema.sql", schema)
        self._write_work_file("manifest.json", manifest_content)

        snapshot_prefix = f"{self._config.aws_s3_dump_prefix}/{dump_dir}"
        self._storage.put_text(
            f"{snapshot_prefix}/schema.sql",
            schema,
            "text/plain; charset=utf-8",
        )
        self._storage.put_text(
            f"{snapshot_prefix}/manifest.json",
            manifest_content,
            "application/json",
        )
        manifest_sha256 = hashlib.sha256(manifest_content.encode()).hexdigest()
        self._publish_dump_index(dump_dir, manifest_sha256)
        log.info("Weekly ClickHouse dump complete")

    def _generate_schema(self) -> tuple[list[ExportTable], str]:
        source_tables = self._clickhouse.source_tables(self._config.dump_tables)
        export_tables: list[ExportTable] = []
        definitions: list[str] = []

        log.info("Generating portable schema")
        for table in source_tables:
            ddl = portable_ddl(
                self._clickhouse.show_create_table(table.name),
                self._config.database_name,
            )
            definitions.append(f"{ddl};\n\n")
            export_tables.append(
                ExportTable(
                    name=table.name,
                    engine=table.engine,
                    parameterized=(
                        table.engine == "View"
                        and PARAMETERIZED_VIEW.search(ddl) is not None
                    ),
                )
            )

        schema = "".join(definitions)
        validate_schema(schema, self._config.database_name)
        return export_tables, schema

    def _export_tables(
        self,
        tables: list[ExportTable],
        s3_base: str,
    ) -> list[dict[str, Any]]:
        manifest_tables: list[dict[str, Any]] = []

        log.info("Exporting %d tables to S3", len(tables))
        for table in tables:
            rows: int | None = None
            if table.parameterized:
                log.info("Skipping parameterized view: %s", table.name)
            else:
                log.info("Exporting table: %s", table.name)
                s3_url = f"{s3_base}/{table.name}.parquet"
                self._clickhouse.export_table(
                    table.name,
                    s3_url,
                    self._credentials,
                )
                rows = self._clickhouse.parquet_row_count(
                    s3_url,
                    self._credentials,
                )

            manifest_tables.append(
                {
                    "name": table.name,
                    "type": table.engine,
                    "row_count": rows,
                }
            )
        return manifest_tables

    def _publish_dump_index(
        self,
        current_dump: str,
        manifest_sha256: str,
    ) -> None:
        all_dumps = clickhouse_inventory(
            self._storage.list_prefix(self._config.aws_s3_dump_prefix)
        )

        expired_dumps = all_dumps[self._config.keep_dumps :]
        log.info(
            "Pruning %d dump(s) beyond the most recent %d",
            len(expired_dumps),
            self._config.keep_dumps,
        )
        for dump in expired_dumps:
            directory = str(dump["dir"])
            if not directory.startswith("dump_"):
                raise RuntimeError(f"Refusing to delete unexpected prefix: {directory}")
            self._storage.delete_prefix(
                f"{self._config.aws_s3_dump_prefix}/{directory}"
            )

        mysql = mysql_inventory(
            self._storage.list_prefix(self._config.aws_s3_mysql_prefix)
        )
        retained_dumps: list[dict[str, Any]] = []
        for dump in all_dumps[: self._config.keep_dumps]:
            item = dict(dump)
            if item["dir"] == current_dump:
                item["manifest_sha256"] = manifest_sha256
            retained_dumps.append(item)

        index = {
            "generated": utc_timestamp(),
            "clickhouse_prefix": self._config.aws_s3_dump_prefix,
            "mysql_prefix": self._config.aws_s3_mysql_prefix,
            "clickhouse": retained_dumps,
            "mysql": mysql,
        }
        content = json_text(index)
        self._write_work_file("dumps.json", content)
        self._storage.put_text(
            "dumps.json",
            content,
            "application/json",
            cache_control="max-age=300",
        )

    def _write_work_file(self, name: str, content: str) -> None:
        path = self._config.work_dir / name
        path.write_text(content, encoding="utf-8")
