import {
  ChevronRight,
  Clock,
  createIcons,
  ExternalLink,
  TriangleAlert,
} from "lucide";

import type {
  ClickHouseDump,
  DumpFile,
  DumpsDocument,
  HuggingFaceMarker,
} from "./types";

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

const MYSQL_FORMATS: Array<[RegExp, string]> = [
  [/\.sql\.gz$/, "SQL (gzip)"],
  [/\.sql$/, "SQL"],
  [/\.tar\.gz$/, "TAR (gzip)"],
  [/\.tar$/, "TAR"],
];

function element(id: string): HTMLElement {
  const value = document.getElementById(id);
  if (!value) {
    throw new Error(`Missing required page element: ${id}`);
  }
  return value;
}

function human(bytes: number): string {
  if (bytes >= 1073741824) {
    return `${(bytes / 1073741824).toFixed(2)} GiB`;
  }
  if (bytes >= 1048576) {
    return `${(bytes / 1048576).toFixed(2)} MiB`;
  }
  if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(2)} KiB`;
  }
  return `${bytes} B`;
}

function escapeHtml(value: unknown): string {
  return String(value).replace(
    /[&<>"]/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
      })[character] ?? character,
  );
}

function parseDumpName(name: string): { date: string; version: string | null } {
  const parts = name.split("_");
  const month = MONTHS[Number(parts[2]) - 1];
  const day = Number(parts[3]);

  if (parts.length < 5 || parts[0] !== "dump" || !month || !day) {
    return { date: name, version: null };
  }

  return {
    date: `${month} ${day}, ${parts[1]}`,
    version: [parts[4].slice(1), ...parts.slice(5)].join("."),
  };
}

function directoryUrl(baseUrl: URL, ...segments: string[]): URL {
  const encodedPath = segments.map(encodeURIComponent).join("/");
  return new URL(`${encodedPath}/`, baseUrl);
}

function fileUrl(baseUrl: URL, filename: string): string {
  return new URL(encodeURIComponent(filename), baseUrl).href;
}

function canExplore(
  marker: HuggingFaceMarker | null,
  dump: ClickHouseDump,
): marker is HuggingFaceMarker {
  return Boolean(
    marker &&
      marker.dump_dir === dump.dir &&
      marker.manifest_sha256 === dump.manifest_sha256,
  );
}

function renderDump(
  dump: ClickHouseDump,
  prefix: string,
  marker: HuggingFaceMarker | null,
  assetBaseUrl: URL,
  isLatest: boolean,
): string {
  const baseUrl = directoryUrl(assetBaseUrl, prefix, dump.dir);
  const metadata = parseDumpName(dump.dir);
  const tables = dump.files.filter((file) => file.name.endsWith(".parquet"));
  const extras = dump.files.filter((file) => !file.name.endsWith(".parquet"));
  const total = dump.files.reduce((sum, file) => sum + file.size, 0);
  const schema = metadata.version
    ? `schema v${escapeHtml(metadata.version)}`
    : null;
  const datasetUrl = canExplore(marker, dump)
    ? marker.dataset_url.replace(/\/$/, "")
    : null;
  const publicationStatus = isLatest
    ? datasetUrl
      ? `Hugging Face: <a class="publication-status published" target="_blank" rel="noopener noreferrer" href="${escapeHtml(datasetUrl)}">Published<i class="publication-status-icon" data-lucide="external-link" aria-hidden="true"></i></a>`
      : 'Hugging Face: <span class="publication-status pending">Pending<i class="publication-status-icon" data-lucide="clock" aria-hidden="true"></i></span>'
    : null;

  let html = "<details>\n";
  html +=
    '  <summary class="details-summary"><i class="details-caret" data-lucide="chevron-right" aria-hidden="true"></i>';
  html += escapeHtml(metadata.date);
  if (schema) {
    html += `<span class="schema-version">${schema}</span>`;
  }
  if (publicationStatus) {
    html += `<span class="meta">${publicationStatus}</span>`;
  }
  html += "</summary>\n";

  if (extras.length > 0) {
    html += '  <p class="extras">';
    for (const file of extras) {
      html += `<a class="file-link" href="${fileUrl(baseUrl, file.name)}" download>`;
      html += `${escapeHtml(file.name)}<span class="size">${human(file.size)}</span></a>`;
    }
    html += "</p>\n";
  }

  html += '  <div class="table-scroll">\n    <table>\n';
  html += `      <thead><tr><th>Table (${tables.length})</th><th>Size (${human(total)})</th><th>Download</th><th>Explore</th></tr></thead>\n`;
  html += "      <tbody>\n";
  for (const file of tables) {
    const table = file.name.replace(/\.parquet$/, "");
    const explore = datasetUrl
      ? `<a class="explore-link" target="_blank" rel="noopener noreferrer" href="${escapeHtml(datasetUrl)}/viewer/${encodeURIComponent(table)}/latest">Data Studio</a>`
      : "&mdash;";
    html += `        <tr><td>${escapeHtml(table)}</td><td>${human(file.size)}</td>`;
    html += `<td><a class="download-link" href="${fileUrl(baseUrl, file.name)}">Download</a></td>`;
    html += `<td>${explore}</td></tr>\n`;
  }
  html += "      </tbody>\n    </table>\n  </div>\n";

  const manifestUrl = fileUrl(baseUrl, "manifest.json");
  html += "  <p>Download every Parquet file in this dump:</p>\n";
  html += `  <pre><code>curl -s ${manifestUrl} \\\n`;
  html += "  | jq -r &#39;.tables[].name&#39; \\\n";
  html += `  | xargs -I{} curl -O ${baseUrl.href}{}.parquet</code></pre>\n`;
  html += "</details>\n";
  return html;
}

function renderMysqlRow(
  file: DumpFile,
  prefix: string,
  assetBaseUrl: URL,
): string {
  let stem = file.name;
  let format = "&mdash;";

  for (const [suffix, label] of MYSQL_FORMATS) {
    if (suffix.test(stem)) {
      format = label;
      stem = stem.replace(suffix, "");
      break;
    }
  }

  const metadata = parseDumpName(stem);
  const prefixUrl = directoryUrl(assetBaseUrl, prefix);
  return (
    `  <tr><td>${escapeHtml(metadata.date)}</td>` +
    `<td>${metadata.version ? `v${escapeHtml(metadata.version)}` : "&mdash;"}</td>` +
    `<td>${format}</td><td>${human(file.size)}</td>` +
    `<td><a class="download-link" href="${fileUrl(prefixUrl, file.name)}">Download</a></td></tr>\n`
  );
}

export function renderPage(
  data: DumpsDocument,
  marker: HuggingFaceMarker | null,
  assetBaseUrl: URL,
): void {
  element("dumps").innerHTML = data.clickhouse
    .map((dump, index) =>
      renderDump(
        dump,
        data.clickhouse_prefix,
        index === 0 ? marker : null,
        assetBaseUrl,
        index === 0,
      ),
    )
    .join("");

  element("mysql").innerHTML =
    data.mysql.length > 0
      ? data.mysql
          .map((file) => renderMysqlRow(file, data.mysql_prefix, assetBaseUrl))
          .join("")
      : '  <tr><td colspan="5">No legacy MySQL dumps available.</td></tr>\n';

  renderIcons();
}

export function renderIcons(): void {
  createIcons({ icons: { ChevronRight, Clock, ExternalLink, TriangleAlert } });
}

export function renderLoadError(error: unknown): void {
  console.error("Failed to load public database export metadata", error);
  element("dumps").innerHTML =
    '<p class="error-message" role="alert">' +
    '<i class="error-icon" data-lucide="triangle-alert" aria-hidden="true"></i>' +
    "<span>Unable to load ClickHouse exports. Please try again later.</span></p>";
  element("mysql").innerHTML =
    '<tr><td class="error-cell" colspan="5">' +
    '<span><i class="error-icon" data-lucide="triangle-alert" aria-hidden="true"></i>' +
    "Unable to load MySQL exports. Please try again later.</span></td></tr>";
  renderIcons();
}
