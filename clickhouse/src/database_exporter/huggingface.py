from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
