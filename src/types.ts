export interface DumpFile {
  name: string;
  size: number;
}

export interface ClickHouseDump {
  dir: string;
  files: DumpFile[];
  manifest_sha256?: string;
}

export interface DumpsDocument {
  clickhouse_prefix: string;
  mysql_prefix: string;
  clickhouse: ClickHouseDump[];
  mysql: DumpFile[];
}

export interface HuggingFaceMarker {
  dump_dir: string;
  manifest_sha256: string;
  dataset_url: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isDumpFile(value: unknown): value is DumpFile {
  return (
    isRecord(value) &&
    typeof value.name === "string" &&
    typeof value.size === "number" &&
    Number.isFinite(value.size) &&
    value.size >= 0
  );
}

function isClickHouseDump(value: unknown): value is ClickHouseDump {
  return (
    isRecord(value) &&
    typeof value.dir === "string" &&
    Array.isArray(value.files) &&
    value.files.every(isDumpFile) &&
    (value.manifest_sha256 === undefined ||
      typeof value.manifest_sha256 === "string")
  );
}

export function isDumpsDocument(value: unknown): value is DumpsDocument {
  return (
    isRecord(value) &&
    typeof value.clickhouse_prefix === "string" &&
    typeof value.mysql_prefix === "string" &&
    Array.isArray(value.clickhouse) &&
    value.clickhouse.every(isClickHouseDump) &&
    Array.isArray(value.mysql) &&
    value.mysql.every(isDumpFile)
  );
}

export function isHuggingFaceMarker(
  value: unknown,
): value is HuggingFaceMarker {
  return (
    isRecord(value) &&
    typeof value.dump_dir === "string" &&
    typeof value.manifest_sha256 === "string" &&
    typeof value.dataset_url === "string"
  );
}
