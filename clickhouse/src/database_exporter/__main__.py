from __future__ import annotations

import argparse
import logging

from botocore.exceptions import BotoCoreError, ClientError
from clickhouse_connect.driver.exceptions import ClickHouseError
from huggingface_hub.errors import HfHubHTTPError

from .clickhouse import ClickHouseDatabase
from .config import Config, ConfigurationError, PublisherConfig
from .exporter import DatabaseExporter
from .huggingface import DatasetViewer, HuggingFaceRepository
from .publisher import HuggingFacePublisher
from .storage import S3Storage


log = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="database-exporter",
        description="Publish cBioPortal public database snapshots",
    )
    parser.add_argument(
        "command",
        choices=("export", "publish-huggingface"),
        default="export",
        nargs="?",
    )
    return parser


def _export() -> None:
    config = Config.from_env()
    storage = S3Storage(config)
    credentials = storage.credentials()
    with ClickHouseDatabase(config) as clickhouse:
        DatabaseExporter(config, clickhouse, storage, credentials).run()


def _publish_huggingface() -> None:
    config = PublisherConfig.from_env()
    storage = S3Storage(config)
    HuggingFacePublisher(
        config,
        storage,
        HuggingFaceRepository(config),
        DatasetViewer(config),
    ).run()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        command = _parser().parse_args().command
        if command == "export":
            _export()
        else:
            _publish_huggingface()
    except (
        BotoCoreError,
        ClickHouseError,
        ClientError,
        ConfigurationError,
        HfHubHTTPError,
        OSError,
        RuntimeError,
    ) as error:
        log.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
