# cBioPortal Database Exporter

## Frontend development

```shell
nvm use
npm ci
npm run dev
```

The development server renders current public dump metadata from the CDN
configured by `VITE_CDN_ORIGIN` in `.env.development`.

Create a static production build:

```shell
npm run build
```

The generated files are written to `dist/`.

## ClickHouse exporter development

Install the locked Python environment:

```shell
cd clickhouse
uv sync --frozen
```

Build the Linux AMD64 image:

```shell
docker build --platform linux/amd64 --tag database-exporter:local clickhouse
```

The exporter requires:

- `AWS_PROFILE`, `AWS_S3_DUMP_BUCKET`, and `AWS_S3_DUMP_PREFIX`
- `CLICKHOUSE_HOST`, `CLICKHOUSE_USER`, and
  `CLICKHOUSE_PASSWORD`
- `PUBLIC_DB_ACTIVE_COLOR`, `BLUE_DB_PORTAL_DB_NAME`, and
  `GREEN_DB_PORTAL_DB_NAME`

Optional settings are `AWS_S3_REGION` (`us-east-1`),
`AWS_S3_MYSQL_PREFIX` (`dumps`), `CLICKHOUSE_SECURE` (`true`),
`CLICKHOUSE_HTTP_PORT` (`8443`), `CLICKHOUSE_TIMEOUT_SECONDS` (`43200`),
`DUMP_TABLES` (all allowed tables), `KEEP_DUMPS` (`5`), and `WORK_DIR`
(`/tmp/dump`). Boto3 uses the standard AWS credentials-file lookup unless
`AWS_SHARED_CREDENTIALS_FILE` is set.

Running the exporter writes database snapshots and metadata to S3 and deletes
ClickHouse dumps beyond `KEEP_DUMPS`.
