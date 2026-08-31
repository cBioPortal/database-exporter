from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError

from .config import Config
from .storage import AwsCredentials


EXCLUDED_TABLES = ("authorities", "data_access_tokens", "users")
EXCLUDED_NAME_PARTS = (
    "_backup",
    "token",
    "oauth",
    "session",
    "login",
    "password",
    "credential",
    "secret",
)


class ExportCommandError(RuntimeError):
    """Raised when a credential-bearing S3 export command fails."""


@dataclass(frozen=True)
class SourceTable:
    name: str
    engine: str


def quote_identifier(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"


class ClickHouseDatabase:
    def __init__(self, config: Config) -> None:
        self._client: Client = clickhouse_connect.get_client(
            host=config.clickhouse_host,
            port=config.clickhouse_http_port,
            username=config.clickhouse_user,
            password=config.clickhouse_password,
            secure=config.clickhouse_secure,
            connect_timeout=30,
            send_receive_timeout=config.clickhouse_timeout_seconds,
        )
        self._database_name = config.database_name

    def __enter__(self) -> ClickHouseDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def schema_version(self) -> str:
        database = quote_identifier(self._database_name)
        return str(
            self._client.command(
                f"SELECT db_schema_version FROM {database}.info LIMIT 1"
            )
        ).strip()

    def server_version(self) -> str:
        return str(self._client.command("SELECT version()")).strip()

    def source_tables(self, selected_tables: tuple[str, ...]) -> list[SourceTable]:
        parameters: dict[str, Any] = {"database": self._database_name}
        exclusions = ", ".join(f"%(excluded_{index})s" for index in range(len(EXCLUDED_TABLES)))
        excluded_parts = ", ".join(
            f"%(excluded_part_{index})s"
            for index in range(len(EXCLUDED_NAME_PARTS))
        )
        parameters.update(
            {
                f"excluded_{index}": value
                for index, value in enumerate(EXCLUDED_TABLES)
            }
        )
        parameters.update(
            {
                f"excluded_part_{index}": value
                for index, value in enumerate(EXCLUDED_NAME_PARTS)
            }
        )

        selection = ""
        if selected_tables:
            placeholders = ", ".join(
                f"%(selected_{index})s" for index in range(len(selected_tables))
            )
            selection = f"\nAND name IN ({placeholders})"
            parameters.update(
                {
                    f"selected_{index}": value
                    for index, value in enumerate(selected_tables)
                }
            )

        result = self._client.query(
            f"""
            SELECT name, engine
            FROM system.tables
            WHERE database = %(database)s
              AND name NOT IN ({exclusions})
              AND NOT multiSearchAny(lower(name), [{excluded_parts}])
              {selection}
            ORDER BY name
            """,
            parameters=parameters,
        )
        return [SourceTable(str(name), str(engine)) for name, engine in result.result_rows]

    def show_create_table(self, table_name: str) -> str:
        database = quote_identifier(self._database_name)
        table = quote_identifier(table_name)
        return str(self._client.command(f"SHOW CREATE TABLE {database}.{table}")).rstrip()

    def export_table(
        self,
        table_name: str,
        s3_url: str,
        credentials: AwsCredentials,
    ) -> None:
        database = quote_identifier(self._database_name)
        table = quote_identifier(table_name)
        try:
            self._client.command(
                f"""
                INSERT INTO FUNCTION s3(
                    %(s3_url)s,
                    %(access_key_id)s,
                    %(secret_access_key)s,
                    %(session_token)s,
                    'Parquet'
                )
                SELECT * FROM {database}.{table}
                SETTINGS s3_truncate_on_insert = 1
                """,
                parameters={
                    "s3_url": s3_url,
                    "access_key_id": credentials.access_key_id,
                    "secret_access_key": credentials.secret_access_key,
                    "session_token": credentials.session_token,
                },
            )
        except ClickHouseError:
            raise ExportCommandError(
                f"ClickHouse failed to export table {table_name}"
            ) from None

    def parquet_row_count(self, s3_url: str) -> int:
        value = self._client.command(
            """
            SELECT count()
            FROM s3(%(s3_url)s, 'Parquet')
            SETTINGS optimize_count_from_files = 1
            """,
            parameters={"s3_url": s3_url},
        )
        try:
            rows = int(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"Invalid Parquet row count: {value!r}") from error
        if rows < 0:
            raise RuntimeError(f"Invalid Parquet row count: {rows}")
        return rows
