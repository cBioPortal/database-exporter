from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError
from clickhouse_connect.driver.exceptions import ClickHouseError

from .clickhouse import ClickHouseDatabase
from .config import Config, ConfigurationError
from .exporter import DatabaseExporter
from .storage import S3Storage


log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = Config.from_env()
        storage = S3Storage(config)
        credentials = storage.credentials()
        with ClickHouseDatabase(config) as clickhouse:
            DatabaseExporter(config, clickhouse, storage, credentials).run()
    except (
        BotoCoreError,
        ClickHouseError,
        ClientError,
        ConfigurationError,
        OSError,
        RuntimeError,
    ) as error:
        log.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
