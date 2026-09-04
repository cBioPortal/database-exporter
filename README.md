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

Run the S3 exporter (the command is optional for backward compatibility):

```shell
cd clickhouse
uv run database-exporter export
```

The S3 exporter requires:

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

## Hugging Face publisher

The same image can publish the latest completed S3 snapshot to the
[`cBioPortal/publicDatabase`](https://huggingface.co/datasets/cBioPortal/publicDatabase)
dataset:

```shell
cd clickhouse
uv run database-exporter publish-huggingface
```

Run this as a separate job only after the S3 exporter succeeds. The publisher
uses `dumps.json` and its latest manifest hash as the source-of-truth snapshot,
uploads each Parquet file to a resumable staging branch, and promotes the
complete dataset to `main` in one commit. It writes `huggingface.json` to the S3
bucket after the verified staging revision has been promoted to `main`. Dataset
Viewer indexing continues asynchronously and is not part of the publication
job.

Required settings:

- `AWS_PROFILE`, `AWS_S3_DUMP_BUCKET`, and `AWS_S3_DUMP_PREFIX`
- `HF_TOKEN`, using a fine-grained token with write access only to the target
  dataset

Optional settings are `AWS_S3_REGION` (`us-east-1`),
`HF_DATASET_REPO` (`cBioPortal/publicDatabase`), `HF_WORK_DIR`
(`/tmp/dump/huggingface`).

For cluster uploads, set `HF_XET_CACHE` to a local disk path and provision the
work/cache volume for the largest Parquet object plus Xet working space. The
publisher removes each local Parquet file after it has been verified on its
staging branch, so it does not need room for the complete snapshot.

The command is idempotent. A failed upload leaves the current Hugging Face
`main` revision and S3 publication marker unchanged; rerunning resumes the
dump-specific staging branch. After successful publication, the staging branch
is deleted.

Before enabling the production schedule, confirm Hugging Face storage capacity
for the public dataset and confirm that the dataset card's AGPL-3.0 label is
appropriate for the exported contents.
