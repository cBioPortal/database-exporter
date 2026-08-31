from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


HUGGING_FACE_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
)


class ConfigurationError(ValueError):
    """Raised when exporter configuration is invalid."""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _positive_integer(environ: Mapping[str, str], name: str, default: int) -> int:
    raw_value = environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive integer") from error
    if value < 1:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _boolean(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw_value = environ.get(name, str(default)).lower()
    if raw_value == "true":
        return True
    if raw_value == "false":
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True)
class Config:
    aws_profile: str
    aws_s3_dump_bucket: str
    aws_s3_dump_prefix: str
    aws_s3_mysql_prefix: str
    aws_s3_region: str
    clickhouse_host: str
    clickhouse_http_port: int
    clickhouse_user: str
    clickhouse_password: str
    clickhouse_secure: bool
    clickhouse_timeout_seconds: int
    database_name: str
    dump_tables: tuple[str, ...]
    keep_dumps: int
    work_dir: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        values = os.environ if environ is None else environ
        active_color = _required(values, "PUBLIC_DB_ACTIVE_COLOR")
        if active_color == "blue":
            database_name = _required(values, "BLUE_DB_PORTAL_DB_NAME")
        elif active_color == "green":
            database_name = _required(values, "GREEN_DB_PORTAL_DB_NAME")
        else:
            raise ConfigurationError(
                f"PUBLIC_DB_ACTIVE_COLOR must be blue or green, got {active_color!r}"
            )

        return cls(
            aws_profile=_required(values, "AWS_PROFILE"),
            aws_s3_dump_bucket=_required(values, "AWS_S3_DUMP_BUCKET"),
            aws_s3_dump_prefix=_required(values, "AWS_S3_DUMP_PREFIX"),
            aws_s3_mysql_prefix=values.get("AWS_S3_MYSQL_PREFIX", "dumps"),
            aws_s3_region=values.get("AWS_S3_REGION", "us-east-1"),
            clickhouse_host=_required(values, "CLICKHOUSE_HOST"),
            clickhouse_http_port=_positive_integer(
                values, "CLICKHOUSE_HTTP_PORT", 8443
            ),
            clickhouse_user=_required(values, "CLICKHOUSE_USER"),
            clickhouse_password=_required(values, "CLICKHOUSE_PASSWORD"),
            clickhouse_secure=_boolean(values, "CLICKHOUSE_SECURE", True),
            clickhouse_timeout_seconds=_positive_integer(
                values, "CLICKHOUSE_TIMEOUT_SECONDS", 43200
            ),
            database_name=database_name,
            dump_tables=tuple(values.get("DUMP_TABLES", "").split()),
            keep_dumps=_positive_integer(values, "KEEP_DUMPS", 5),
            work_dir=Path(values.get("WORK_DIR", "/tmp/dump")),
        )


@dataclass(frozen=True)
class PublisherConfig:
    aws_profile: str
    aws_s3_dump_bucket: str
    aws_s3_dump_prefix: str
    aws_s3_region: str
    hf_dataset_repo: str
    hf_token: str
    hf_viewer_poll_seconds: int
    hf_viewer_timeout_seconds: int
    hf_viewer_url: str
    work_dir: Path

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> PublisherConfig:
        values = os.environ if environ is None else environ
        repository = values.get(
            "HF_DATASET_REPO",
            "cBioPortal/publicDatabase",
        )
        if HUGGING_FACE_REPOSITORY.fullmatch(repository) is None:
            raise ConfigurationError(
                "HF_DATASET_REPO must use the namespace/repository format"
            )

        viewer_url = values.get(
            "HF_VIEWER_URL",
            "https://datasets-server.huggingface.co",
        ).rstrip("/")
        if not viewer_url.startswith("https://"):
            raise ConfigurationError("HF_VIEWER_URL must use https")

        return cls(
            aws_profile=_required(values, "AWS_PROFILE"),
            aws_s3_dump_bucket=_required(values, "AWS_S3_DUMP_BUCKET"),
            aws_s3_dump_prefix=_required(values, "AWS_S3_DUMP_PREFIX"),
            aws_s3_region=values.get("AWS_S3_REGION", "us-east-1"),
            hf_dataset_repo=repository,
            hf_token=_required(values, "HF_TOKEN"),
            hf_viewer_poll_seconds=_positive_integer(
                values,
                "HF_VIEWER_POLL_SECONDS",
                30,
            ),
            hf_viewer_timeout_seconds=_positive_integer(
                values,
                "HF_VIEWER_TIMEOUT_SECONDS",
                7200,
            ),
            hf_viewer_url=viewer_url,
            work_dir=Path(
                values.get(
                    "HF_WORK_DIR",
                    "/tmp/dump/huggingface",
                )
            ),
        )
