from __future__ import annotations

from dataclasses import dataclass

import boto3

from .config import Config


class StorageError(RuntimeError):
    """Raised when S3 credentials or mutations are invalid."""


@dataclass(frozen=True)
class AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str


@dataclass(frozen=True)
class S3Object:
    relative_key: str
    size: int


class S3Storage:
    def __init__(self, config: Config) -> None:
        self._bucket = config.aws_s3_dump_bucket
        self._session = boto3.Session(
            profile_name=config.aws_profile,
            region_name=config.aws_s3_region,
        )
        self._client = self._session.client("s3")

    def credentials(self) -> AwsCredentials:
        credentials = self._session.get_credentials()
        if credentials is None:
            raise StorageError("AWS profile did not provide credentials")
        frozen = credentials.get_frozen_credentials()
        if not frozen.access_key or not frozen.secret_key or not frozen.token:
            raise StorageError("AWS profile must provide temporary session credentials")
        return AwsCredentials(
            access_key_id=frozen.access_key,
            secret_access_key=frozen.secret_key,
            session_token=frozen.token,
        )

    def put_text(
        self,
        key: str,
        value: str,
        content_type: str,
        cache_control: str | None = None,
    ) -> None:
        request: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": value.encode(),
            "ContentType": content_type,
        }
        if cache_control is not None:
            request["CacheControl"] = cache_control
        self._client.put_object(**request)

    def list_prefix(self, prefix: str) -> list[S3Object]:
        key_prefix = f"{prefix}/"
        objects: list[S3Object] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=key_prefix):
            for item in page.get("Contents", []):
                key = item.get("Key")
                size = item.get("Size")
                if isinstance(key, str) and isinstance(size, int):
                    objects.append(
                        S3Object(
                            relative_key=key.removeprefix(key_prefix),
                            size=size,
                        )
                    )
        return objects

    def delete_prefix(self, prefix: str) -> None:
        paginator = self._client.get_paginator("list_objects_v2")
        pending: list[dict[str, str]] = []
        for page in paginator.paginate(
            Bucket=self._bucket,
            Prefix=f"{prefix}/",
        ):
            for item in page.get("Contents", []):
                key = item.get("Key")
                if isinstance(key, str):
                    pending.append({"Key": key})
                if len(pending) == 1000:
                    self._delete_objects(pending)
                    pending = []
        if pending:
            self._delete_objects(pending)

    def _delete_objects(self, objects: list[dict[str, str]]) -> None:
        response = self._client.delete_objects(
            Bucket=self._bucket,
            Delete={"Objects": objects, "Quiet": True},
        )
        errors = response.get("Errors", [])
        if errors:
            raise StorageError(f"S3 failed to delete {len(errors)} object(s)")
