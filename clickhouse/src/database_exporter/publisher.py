from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import PublisherConfig
from .huggingface import (
    MANAGED_METADATA_FILES,
    DatasetViewer,
    HuggingFaceRepository,
    dataset_card,
    json_text,
)
from .storage import S3Storage


log = logging.getLogger(__name__)
SAFE_DUMP_DIRECTORY = re.compile(
    r"^dump_\d{4}_\d{2}_\d{2}_v[A-Za-z0-9_]+$"
)
SAFE_TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PublicationFile:
    source_key: str
    path_in_repo: str
    size: int
    table_name: str


@dataclass(frozen=True)
class SourceSnapshot:
    clickhouse_version: str
    dump_dir: str
    files: tuple[PublicationFile, ...]
    manifest_content: str
    manifest_sha256: str
    schema_content: str
    schema_version: str
    source_url: str

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(item.table_name for item in self.files)


class HuggingFacePublisher:
    def __init__(
        self,
        config: PublisherConfig,
        storage: S3Storage,
        repository: HuggingFaceRepository,
        viewer: DatasetViewer,
    ) -> None:
        self._config = config
        self._repository = repository
        self._storage = storage
        self._viewer = viewer

    def run(self) -> None:
        self._config.work_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self._load_snapshot()
        self._repository.verify_writable_public_dataset()
        stage_branch = self._repository.stage_branch(
            snapshot.dump_dir,
            snapshot.manifest_sha256,
        )

        main_publication = self._repository.publication("main")
        main_digests = self._publication_digests(
            main_publication,
            snapshot,
        )
        expected_sizes = {
            item.path_in_repo: item.size for item in snapshot.files
        }
        if (
            main_digests is not None
            and self._repository.data_files_match(
                "main",
                expected_sizes,
                main_digests,
            )
        ):
            promoted_revision = self._repository.main_revision()
            log.info(
                "Hugging Face main already contains %s",
                snapshot.dump_dir,
            )
        else:
            promoted_revision = self._stage_and_promote(
                snapshot,
                stage_branch,
            )

        flags = self._viewer.wait_until_ready(snapshot.table_names)
        if self._repository.main_revision() != promoted_revision:
            raise RuntimeError(
                "Hugging Face main changed while Dataset Viewer was indexing"
            )

        self._repository.delete_stage(stage_branch)
        existing_marker = self._load_marker()
        if (
            self._matches_snapshot(existing_marker, snapshot)
            and existing_marker.get("commit_sha") == promoted_revision
        ):
            log.info("Hugging Face publication marker is already current")
            return
        marker = {
            "commit_sha": promoted_revision,
            "dataset_url": self._repository.dataset_url,
            "dump_dir": snapshot.dump_dir,
            "manifest_sha256": snapshot.manifest_sha256,
            "published_at": utc_timestamp(),
            "viewer_features": flags,
        }
        self._storage.put_text(
            "huggingface.json",
            json_text(marker),
            "application/json",
            cache_control="max-age=300",
        )
        log.info(
            "Hugging Face publication complete: %s",
            promoted_revision,
        )

    def _stage_and_promote(
        self,
        snapshot: SourceSnapshot,
        stage_branch: str,
    ) -> str:
        self._repository.create_stage(stage_branch)
        digests: dict[str, str] = {}
        downloads = self._config.work_dir / "downloads"

        for item in snapshot.files:
            digest = self._repository.staged_file_digest(
                stage_branch,
                snapshot.dump_dir,
                snapshot.manifest_sha256,
                item.path_in_repo,
                item.size,
            )
            if digest is not None:
                log.info("Already staged: %s", item.path_in_repo)
                digests[item.path_in_repo] = digest
                continue

            local_path = downloads / Path(item.path_in_repo).name
            log.info(
                "Downloading from S3: %s (%d bytes)",
                item.source_key,
                item.size,
            )
            try:
                digest = self._storage.download(
                    item.source_key,
                    local_path,
                    item.size,
                )
                log.info("Uploading to Hugging Face: %s", item.path_in_repo)
                self._repository.upload_staged_file(
                    stage_branch,
                    snapshot.dump_dir,
                    snapshot.manifest_sha256,
                    local_path,
                    item.path_in_repo,
                    item.size,
                    digest,
                )
                digests[item.path_in_repo] = digest
            finally:
                local_path.unlink(missing_ok=True)

        published_at = utc_timestamp()
        publication = {
            "clickhouse_version": snapshot.clickhouse_version,
            "dump_dir": snapshot.dump_dir,
            "files": [
                {
                    "path": item.path_in_repo,
                    "sha256": digests[item.path_in_repo],
                    "size": item.size,
                    "source_key": item.source_key,
                }
                for item in snapshot.files
            ],
            "manifest_sha256": snapshot.manifest_sha256,
            "published_at": published_at,
            "schema_version": snapshot.schema_version,
            "source_url": snapshot.source_url,
        }
        metadata = {
            "README.md": dataset_card(
                self._config.hf_dataset_repo,
                snapshot.dump_dir,
                snapshot.source_url,
                snapshot.schema_version,
                snapshot.clickhouse_version,
                snapshot.table_names,
            ),
            "manifest.json": snapshot.manifest_content,
            "publication.json": json_text(publication),
            "schema.sql": snapshot.schema_content,
        }
        self._repository.upload_stage_metadata(stage_branch, metadata)

        expected_sizes = {
            item.path_in_repo: item.size for item in snapshot.files
        }
        staging_revision = self._repository.verify_stage(
            stage_branch,
            expected_sizes,
            digests,
        )
        expected_paths = set(expected_sizes) | set(MANAGED_METADATA_FILES)
        log.info(
            "Promoting staging revision %s to Hugging Face main",
            staging_revision,
        )
        return self._repository.promote(staging_revision, expected_paths)

    def _load_snapshot(self) -> SourceSnapshot:
        index = self._json_object(
            self._storage.get_text("dumps.json"),
            "dumps.json",
        )
        dumps = index.get("clickhouse")
        if not isinstance(dumps, list) or not dumps:
            raise RuntimeError("dumps.json has no ClickHouse snapshots")
        current = dumps[0]
        if not isinstance(current, dict):
            raise RuntimeError("dumps.json has an invalid current snapshot")

        dump_dir = current.get("dir")
        manifest_sha256 = current.get("manifest_sha256")
        if (
            not isinstance(dump_dir, str)
            or SAFE_DUMP_DIRECTORY.fullmatch(dump_dir) is None
        ):
            raise RuntimeError("dumps.json has an invalid dump directory")
        if (
            not isinstance(manifest_sha256, str)
            or SHA256.fullmatch(manifest_sha256) is None
        ):
            raise RuntimeError(
                "Latest ClickHouse snapshot is missing a valid manifest hash"
            )

        prefix = self._config.aws_s3_dump_prefix
        snapshot_prefix = f"{prefix}/{dump_dir}"
        objects = self._index_objects(current.get("files"))
        manifest_size = objects.get("manifest.json")
        schema_size = objects.get("schema.sql")
        if manifest_size is None or schema_size is None:
            raise RuntimeError(
                "Current snapshot is missing manifest.json or schema.sql"
            )

        manifest_content = self._storage.get_text(
            f"{snapshot_prefix}/manifest.json"
        )
        if len(manifest_content.encode()) != manifest_size:
            raise RuntimeError("manifest.json size does not match dumps.json")
        actual_manifest_sha256 = hashlib.sha256(
            manifest_content.encode()
        ).hexdigest()
        if actual_manifest_sha256 != manifest_sha256:
            raise RuntimeError("manifest.json hash does not match dumps.json")
        manifest = self._json_object(manifest_content, "manifest.json")

        schema_content = self._storage.get_text(
            f"{snapshot_prefix}/schema.sql"
        )
        if len(schema_content.encode()) != schema_size:
            raise RuntimeError("schema.sql size does not match dumps.json")

        table_names = self._manifest_tables(manifest.get("tables"))
        parquet_names = {
            name.removesuffix(".parquet")
            for name in objects
            if name.endswith(".parquet")
        }
        if parquet_names != table_names:
            missing = sorted(table_names - parquet_names)
            unexpected = sorted(parquet_names - table_names)
            raise RuntimeError(
                "Parquet inventory does not match manifest tables "
                f"(missing={missing}, unexpected={unexpected})"
            )

        files = tuple(
            PublicationFile(
                source_key=f"{snapshot_prefix}/{table_name}.parquet",
                path_in_repo=f"data/{table_name}.parquet",
                size=objects[f"{table_name}.parquet"],
                table_name=table_name,
            )
            for table_name in sorted(table_names)
        )
        schema_version = manifest.get("schema_version")
        clickhouse_version = manifest.get("clickhouse_version")
        if not isinstance(schema_version, str) or not schema_version:
            raise RuntimeError("manifest.json has an invalid schema version")
        if not isinstance(clickhouse_version, str) or not clickhouse_version:
            raise RuntimeError("manifest.json has an invalid ClickHouse version")

        source_path = "/".join(
            quote(part, safe="")
            for part in (*prefix.split("/"), dump_dir)
            if part
        )
        source_url = (
            f"https://{self._config.aws_s3_dump_bucket}.s3."
            f"{self._config.aws_s3_region}.amazonaws.com/{source_path}"
        )
        log.info(
            "Publishing S3 snapshot %s with %d tables",
            dump_dir,
            len(files),
        )
        return SourceSnapshot(
            clickhouse_version=clickhouse_version,
            dump_dir=dump_dir,
            files=files,
            manifest_content=manifest_content,
            manifest_sha256=manifest_sha256,
            schema_content=schema_content,
            schema_version=schema_version,
            source_url=source_url,
        )

    @staticmethod
    def _json_object(content: str, name: str) -> dict[str, Any]:
        try:
            value = json.loads(content)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{name} is not valid JSON") from error
        if not isinstance(value, dict):
            raise RuntimeError(f"{name} must contain a JSON object")
        return value

    @staticmethod
    def _index_objects(value: Any) -> dict[str, int]:
        if not isinstance(value, list):
            raise RuntimeError("Current snapshot has an invalid file inventory")
        objects: dict[str, int] = {}
        for item in value:
            if not isinstance(item, dict):
                raise RuntimeError(
                    "Current snapshot has an invalid file inventory"
                )
            name = item.get("name")
            size = item.get("size")
            if (
                not isinstance(name, str)
                or "/" in name
                or name in objects
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                raise RuntimeError(
                    "Current snapshot has an invalid file inventory"
                )
            objects[name] = size
        return objects

    @staticmethod
    def _manifest_tables(value: Any) -> set[str]:
        if not isinstance(value, list):
            raise RuntimeError("manifest.json has an invalid table inventory")
        tables: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise RuntimeError(
                    "manifest.json has an invalid table inventory"
                )
            name = item.get("name")
            rows = item.get("row_count")
            if rows is None:
                continue
            if (
                not isinstance(name, str)
                or SAFE_TABLE_NAME.fullmatch(name) is None
                or name in tables
                or not isinstance(rows, int)
                or isinstance(rows, bool)
                or rows < 0
            ):
                raise RuntimeError(
                    "manifest.json has an invalid table inventory"
                )
            tables.add(name)
        if not tables:
            raise RuntimeError("manifest.json has no exported tables")
        return tables

    @staticmethod
    def _matches_snapshot(
        publication: Mapping[str, Any] | None,
        snapshot: SourceSnapshot,
    ) -> bool:
        return bool(
            publication
            and publication.get("dump_dir") == snapshot.dump_dir
            and publication.get("manifest_sha256")
            == snapshot.manifest_sha256
        )

    def _load_marker(self) -> dict[str, Any] | None:
        content = self._storage.get_optional_text("huggingface.json")
        if content is None:
            return None
        return self._json_object(content, "huggingface.json")

    @classmethod
    def _publication_digests(
        cls,
        publication: Mapping[str, Any] | None,
        snapshot: SourceSnapshot,
    ) -> dict[str, str] | None:
        if not cls._matches_snapshot(publication, snapshot):
            return None
        value = publication.get("files") if publication else None
        if not isinstance(value, list):
            return None
        expected = {item.path_in_repo for item in snapshot.files}
        digests: dict[str, str] = {}
        for item in value:
            if not isinstance(item, dict):
                return None
            path = item.get("path")
            digest = item.get("sha256")
            if (
                not isinstance(path, str)
                or path in digests
                or not isinstance(digest, str)
                or SHA256.fullmatch(digest) is None
            ):
                return None
            digests[path] = digest
        return digests if set(digests) == expected else None
