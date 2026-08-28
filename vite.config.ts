import { defineConfig, loadEnv } from "vite";

import { DEVELOPMENT_PROXY_PATH } from "./src/config.ts";

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, ".", "VITE_");
  const liveCdnOrigin = env.VITE_CDN_ORIGIN;
  if (command === "serve" && mode === "development" && !liveCdnOrigin) {
    throw new Error("VITE_CDN_ORIGIN is required");
  }

  return {
    server: liveCdnOrigin
      ? {
          proxy: {
            [DEVELOPMENT_PROXY_PATH]: {
              target: liveCdnOrigin,
              changeOrigin: true,
              rewrite: (path) => path.slice(DEVELOPMENT_PROXY_PATH.length),
            },
          },
        }
      : undefined,
  };
});
