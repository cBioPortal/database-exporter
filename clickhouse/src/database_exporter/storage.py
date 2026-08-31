from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3
from botocore.exceptions import ClientError


class S3Config(Protocol):
    aws_profile: str
    aws_s3_dump_bucket: str
    aws_s3_region: str


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
    def __init__(self, config: S3Config) -> None:
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

    def get_text(self, key: str) -> str:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"]
        try:
            value = body.read()
        finally:
            body.close()
        if not isinstance(value, bytes):
            raise StorageError(f"S3 returned invalid content for {key}")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise StorageError(f"S3 object is not UTF-8 text: {key}") from error

    def get_optional_text(self, key: str) -> str | None:
        try:
            return self.get_text(key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                return None
            raise

    def download(
        self,
        key: str,
        destination: Path,
        expected_size: int,
    ) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f"{destination.name}.part")
        temporary.unlink(missing_ok=True)

        response = self._client.get_object(Bucket=self._bucket, Key=key)
        size = response.get("ContentLength")
        if size != expected_size:
            response["Body"].close()
            raise StorageError(
                f"S3 object size mismatch for {key}: "
                f"expected {expected_size}, got {size}"
            )

        digest = hashlib.sha256()
        written = 0
        body = response["Body"]
        try:
            with temporary.open("wb") as output:
                for chunk in body.iter_chunks(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        output.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
            if written != expected_size:
                raise StorageError(
                    f"S3 download size mismatch for {key}: "
                    f"expected {expected_size}, got {written}"
                )
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            body.close()
        return digest.hexdigest()

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
