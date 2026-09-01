from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from huggingface_hub import (
    CommitOperationAdd,
    CommitOperationCopy,
    CommitOperationDelete,
    HfApi,
)

from .config import PublisherConfig


log = logging.getLogger(__name__)
REPO_TYPE = "dataset"
LATEST_SPLIT = "latest"
MANAGED_METADATA_FILES = (
    "README.md",
    "manifest.json",
    "publication.json",
    "schema.sql",
)


def json_text(value: Any) -> str:
    return f"{json.dumps(value, indent=2, sort_keys=True)}\n"


def dataset_card(
    repository: str,
    schema_version: str,
    clickhouse_version: str,
    table_names: Sequence[str],
) -> str:
    lines = [
        "---",
        "license: agpl-3.0",
        "language:",
        "- en",
        "pretty_name: cBioPortal Public Database",
        "configs:",
    ]
    for table_name in sorted(table_names):
        lines.extend(
            [
                f"- config_name: {json.dumps(table_name)}",
                *(
                    ["  default: true"]
                    if table_name == "cancer_study"
                    else []
                ),
                "  data_files:",
                f"  - split: {LATEST_SPLIT}",
                f"    path: {json.dumps(f'data/{table_name}.parquet')}",
            ]
        )
    lines.extend(
        [
            "---",
            "",
            "# cBioPortal Public Database",
            "",
            "A weekly Parquet snapshot of the public cBioPortal ClickHouse database.",
            "Each database table is exposed as a separate dataset configuration so",
            "it can be browsed independently in the Hugging Face Dataset Viewer.",
            "",
            "## Current snapshot",
            "",
            f"- Database schema version: `{schema_version}`",
            f"- ClickHouse version: `{clickhouse_version}`",
            f"- Tables: {len(table_names)}",
            "",
            "Only the latest successfully published snapshot is present on `main`.",
            "Source files and past snapshots are available from the",
            "[cBioPortal Public Database Dumps](https://public-db-dump.assets.cbioportal.org/).",
            "",
            "## Usage",
            "",
            "Choose a table name as the dataset configuration:",
            "",
            "```python",
            "from datasets import load_dataset",
            "",
            f'dataset = load_dataset("{repository}", "cancer_study", split="latest")',
            "```",
            "",
            "The repository also contains:",
            "",
            "- `manifest.json` with source row counts and version metadata",
            "- `schema.sql` with the portable ClickHouse schema",
            "- `publication.json` with source identity and file integrity metadata",
            "",
            "## Contact",
            "",
            "For help, contact",
            "[cbioportal@googlegroups.com](mailto:cbioportal@googlegroups.com).",
            "",
        ]
    )
    return "\n".join(lines)


class HuggingFaceRepository:
    def __init__(self, config: PublisherConfig) -> None:
        self._repository = config.hf_dataset_repo
        self._token = config.hf_token
        self._cache_dir = config.work_dir / "hub-cache"
        self._api = HfApi(
            token=config.hf_token,
            library_name="cbioportal-database-exporter",
        )

    @property
    def dataset_url(self) -> str:
        return f"https://huggingface.co/datasets/{self._repository}"

    def verify_writable_public_dataset(self) -> None:
        self._api.auth_check(
            self._repository,
            repo_type=REPO_TYPE,
            write=True,
        )
        info = self._api.dataset_info(
            self._repository,
            files_metadata=True,
        )
        if info.private:
            raise RuntimeError(
                f"Hugging Face dataset must be public: {self._repository}"
            )
        if info.disabled:
            raise RuntimeError(
                f"Hugging Face dataset is disabled: {self._repository}"
            )
        if info.gated:
            raise RuntimeError(
                f"Hugging Face dataset must not be gated: {self._repository}"
            )

    def stage_branch(self, dump_dir: str, manifest_sha256: str) -> str:
        return f"publish-{dump_dir}-{manifest_sha256[:12]}"

    def create_stage(self, branch: str) -> None:
        self._api.create_branch(
            self._repository,
            branch=branch,
            revision="main",
            repo_type=REPO_TYPE,
            exist_ok=True,
        )

    def branch_exists(self, branch: str) -> bool:
        refs = self._api.list_repo_refs(
            self._repository,
            repo_type=REPO_TYPE,
        )
        return any(item.name == branch for item in refs.branches)

    def delete_stage(self, branch: str) -> None:
        if self.branch_exists(branch):
            self._api.delete_branch(
                self._repository,
                branch=branch,
                repo_type=REPO_TYPE,
            )

    def publication(self, revision: str) -> Mapping[str, Any] | None:
        if not self._api.file_exists(
            self._repository,
            "publication.json",
            repo_type=REPO_TYPE,
            revision=revision,
        ):
            return None
        path = self._api.hf_hub_download(
            self._repository,
            "publication.json",
            repo_type=REPO_TYPE,
            revision=revision,
            cache_dir=self._cache_dir,
            force_download=True,
        )
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise RuntimeError(
                f"Invalid publication.json on Hugging Face revision {revision}"
            ) from error
        if not isinstance(value, dict):
            raise RuntimeError(
                f"Invalid publication.json on Hugging Face revision {revision}"
            )
        return value

    def staged_file_digest(
        self,
        branch: str,
        dump_dir: str,
        manifest_sha256: str,
        path_in_repo: str,
        expected_size: int,
    ) -> str | None:
        marker_path = self._stage_marker_path(path_in_repo)
        if not self._api.file_exists(
            self._repository,
            marker_path,
            repo_type=REPO_TYPE,
            revision=branch,
        ):
            return None

        path = self._api.hf_hub_download(
            self._repository,
            marker_path,
            repo_type=REPO_TYPE,
            revision=branch,
            cache_dir=self._cache_dir,
            force_download=True,
        )
        try:
            marker = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(marker, dict):
            return None
        digest = marker.get("sha256")
        if (
            marker.get("dump_dir") != dump_dir
            or marker.get("manifest_sha256") != manifest_sha256
            or marker.get("path") != path_in_repo
            or marker.get("size") != expected_size
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            return None
        if not self._remote_file_matches(
            branch,
            path_in_repo,
            expected_size,
            digest,
        ):
            return None
        return digest

    def upload_staged_file(
        self,
        branch: str,
        dump_dir: str,
        manifest_sha256: str,
        local_path: Path,
        path_in_repo: str,
        expected_size: int,
        digest: str,
    ) -> None:
        marker = json_text(
            {
                "dump_dir": dump_dir,
                "manifest_sha256": manifest_sha256,
                "path": path_in_repo,
                "sha256": digest,
                "size": expected_size,
            }
        ).encode()
        parent = self._api.dataset_info(
            self._repository,
            revision=branch,
        ).sha
        self._api.create_commit(
            self._repository,
            operations=[
                CommitOperationAdd(path_in_repo, local_path),
                CommitOperationAdd(
                    self._stage_marker_path(path_in_repo),
                    marker,
                ),
            ],
            commit_message=f"Stage {path_in_repo}",
            repo_type=REPO_TYPE,
            revision=branch,
            parent_commit=parent,
        )
        if not self._remote_file_matches(
            branch,
            path_in_repo,
            expected_size,
            digest,
        ):
            raise RuntimeError(
                f"Hugging Face staged file verification failed: {path_in_repo}"
            )

    def upload_stage_metadata(
        self,
        branch: str,
        contents: Mapping[str, str],
    ) -> None:
        parent = self._api.dataset_info(
            self._repository,
            revision=branch,
        ).sha
        self._api.create_commit(
            self._repository,
            operations=[
                CommitOperationAdd(path, content.encode())
                for path, content in sorted(contents.items())
            ],
            commit_message="Stage dataset metadata",
            repo_type=REPO_TYPE,
            revision=branch,
            parent_commit=parent,
        )

    def verify_stage(
        self,
        branch: str,
        expected_sizes: Mapping[str, int],
        digests: Mapping[str, str],
    ) -> str:
        inventory = self._remote_inventory(branch)
        for path_in_repo, expected_size in expected_sizes.items():
            digest = digests.get(path_in_repo)
            if digest is None or inventory.get(path_in_repo) != (
                expected_size,
                digest,
            ):
                raise RuntimeError(
                    f"Hugging Face staging verification failed: {path_in_repo}"
                )
        files = set(
            self._api.list_repo_files(
                self._repository,
                revision=branch,
                repo_type=REPO_TYPE,
            )
        )
        missing = set(MANAGED_METADATA_FILES) - files
        if missing:
            raise RuntimeError(
                "Hugging Face staging metadata is incomplete: "
                f"{', '.join(sorted(missing))}"
            )
        return self._api.dataset_info(
            self._repository,
            revision=branch,
        ).sha

    def data_files_match(
        self,
        revision: str,
        expected_sizes: Mapping[str, int],
        digests: Mapping[str, str],
    ) -> bool:
        inventory = self._remote_inventory(revision)
        return all(
            digests.get(path) is not None
            and inventory.get(path) == (size, digests[path])
            for path, size in expected_sizes.items()
        )

    def promote(
        self,
        staging_revision: str,
        expected_paths: set[str],
    ) -> str:
        main_info = self._api.dataset_info(
            self._repository,
            revision="main",
        )
        current_files = set(
            self._api.list_repo_files(
                self._repository,
                revision="main",
                repo_type=REPO_TYPE,
            )
        )
        stale_files = sorted(
            path
            for path in current_files
            if path.startswith("data/") and path not in expected_paths
        )
        operations = [
            CommitOperationCopy(
                src_path_in_repo=path,
                path_in_repo=path,
                src_revision=staging_revision,
            )
            for path in sorted(expected_paths)
        ]
        operations.extend(CommitOperationDelete(path) for path in stale_files)
        if len(operations) > 100:
            raise RuntimeError(
                "Atomic Hugging Face promotion exceeds 100 file operations"
            )
        result = self._api.create_commit(
            self._repository,
            operations=operations,
            commit_message="Publish latest public database snapshot",
            repo_type=REPO_TYPE,
            revision="main",
            parent_commit=main_info.sha,
        )
        return result.oid

    def main_revision(self) -> str:
        return self._api.dataset_info(
            self._repository,
            revision="main",
        ).sha

    def _remote_file_matches(
        self,
        revision: str,
        path_in_repo: str,
        expected_size: int,
        digest: str,
    ) -> bool:
        return self._remote_inventory(revision).get(path_in_repo) == (
            expected_size,
            digest,
        )

    def _remote_inventory(
        self,
        revision: str,
    ) -> dict[str, tuple[int, str]]:
        info = self._api.dataset_info(
            self._repository,
            revision=revision,
            files_metadata=True,
        )
        inventory: dict[str, tuple[int, str]] = {}
        for sibling in info.siblings or []:
            lfs = sibling.lfs
            digest = lfs.get("sha256") if isinstance(lfs, dict) else None
            if isinstance(sibling.size, int) and isinstance(digest, str):
                inventory[sibling.rfilename] = (sibling.size, digest)
        return inventory

    @staticmethod
    def _stage_marker_path(path_in_repo: str) -> str:
        return f".publish/{path_in_repo}.json"


class DatasetViewer:
    def __init__(self, config: PublisherConfig) -> None:
        self._base_url = config.hf_viewer_url
        self._poll_seconds = config.hf_viewer_poll_seconds
        self._repository = config.hf_dataset_repo
        self._timeout_seconds = config.hf_viewer_timeout_seconds

    def wait_until_ready(self, table_names: Sequence[str]) -> Mapping[str, bool]:
        deadline = time.monotonic() + self._timeout_seconds
        expected = {
            (table_name, LATEST_SPLIT)
            for table_name in table_names
        }
        log.info(
            "Waiting for Hugging Face Dataset Viewer to index %d configs",
            len(expected),
        )
        while True:
            response = self._request(
                "splits",
                {"dataset": self._repository},
            )
            if response is not None:
                failed = response.get("failed", [])
                if isinstance(failed, list) and failed:
                    names = sorted(
                        str(item.get("config"))
                        for item in failed
                        if isinstance(item, dict)
                    )
                    raise RuntimeError(
                        "Hugging Face Dataset Viewer failed configs: "
                        f"{', '.join(names)}"
                    )
                splits = response.get("splits")
                if isinstance(splits, list):
                    actual = {
                        (str(item.get("config")), str(item.get("split")))
                        for item in splits
                        if isinstance(item, dict)
                    }
                    if actual == expected:
                        break
            self._wait_or_timeout(deadline, "dataset splits")

        pending = {table_name for table_name in table_names}
        while pending:
            ready: set[str] = set()
            for table_name in sorted(pending):
                response = self._request(
                    "first-rows",
                    {
                        "config": table_name,
                        "dataset": self._repository,
                        "split": LATEST_SPLIT,
                    },
                )
                if response is not None:
                    if (
                        response.get("config") == table_name
                        and response.get("split") == LATEST_SPLIT
                        and isinstance(response.get("rows"), list)
                    ):
                        ready.add(table_name)
            pending -= ready
            if pending:
                self._wait_or_timeout(
                    deadline,
                    f"{len(pending)} dataset previews",
                )

        while True:
            flags = self._request(
                "is-valid",
                {"dataset": self._repository},
            )
            if flags is not None and flags.get("preview") is True:
                normalized = {
                    name: value
                    for name, value in flags.items()
                    if isinstance(name, str) and isinstance(value, bool)
                }
                unavailable = sorted(
                    name for name, value in normalized.items() if not value
                )
                if unavailable:
                    log.warning(
                        "Hugging Face features not available: %s",
                        ", ".join(unavailable),
                    )
                return normalized
            self._wait_or_timeout(deadline, "dataset validity")

    def _request(
        self,
        path: str,
        parameters: Mapping[str, str],
    ) -> Mapping[str, Any] | None:
        url = f"{self._base_url}/{path}?{urlencode(parameters)}"
        request = Request(
            url,
            headers={"User-Agent": "cbioportal-database-exporter"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                value = json.loads(response.read())
        except HTTPError as error:
            if error.code in {404, 500, 502, 503, 504}:
                return None
            raise RuntimeError(
                f"Hugging Face Dataset Viewer returned HTTP {error.code}"
            ) from error
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Hugging Face Dataset Viewer returned invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise RuntimeError(
                "Hugging Face Dataset Viewer returned an invalid response"
            )
        return value

    def _wait_or_timeout(self, deadline: float, resource: str) -> None:
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out waiting for Hugging Face {resource}"
            )
        time.sleep(self._poll_seconds)
