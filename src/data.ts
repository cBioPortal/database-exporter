import {
  isDumpsDocument,
  isHuggingFaceMarker,
  type DumpsDocument,
  type HuggingFaceMarker,
} from "./types";

import { DEVELOPMENT_PROXY_PATH } from "./config";

export interface PublicData {
  dumps: DumpsDocument;
  huggingFace: HuggingFaceMarker | null;
  assetBaseUrl: URL;
}

async function fetchJson(url: URL): Promise<unknown> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json();
}

function metadataBaseUrl(): URL {
  return import.meta.env.DEV
    ? new URL(`${DEVELOPMENT_PROXY_PATH}/`, window.location.origin)
    : new URL("./", window.location.href);
}

function assetBaseUrl(): URL {
  if (!import.meta.env.DEV) {
    return new URL("./", window.location.href);
  }

  const liveCdnOrigin = import.meta.env.VITE_CDN_ORIGIN;
  if (!liveCdnOrigin) {
    throw new Error("VITE_CDN_ORIGIN is required");
  }
  return new URL(liveCdnOrigin);
}

export async function loadPublicData(): Promise<PublicData> {
  const metadataBase = metadataBaseUrl();
  const dumpsRequest = fetchJson(new URL("dumps.json", metadataBase)).then(
    (value) => {
      if (!isDumpsDocument(value)) {
        throw new Error("Invalid dumps.json response");
      }
      return value;
    },
  );
  const huggingFaceRequest = fetchJson(
    new URL("huggingface.json", metadataBase),
  )
    .then((value) => (isHuggingFaceMarker(value) ? value : null))
    .catch(() => null);

  const [dumps, huggingFace] = await Promise.all([
    dumpsRequest,
    huggingFaceRequest,
  ]);

  return {
    dumps,
    huggingFace,
    assetBaseUrl: assetBaseUrl(),
  };
}
