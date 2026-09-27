import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// InkDrop's server (inkdrop_web.py) is a hand-rolled http.server app, not a
// framework with a manifest reader. It serves static JS/CSS by fixed path and
// cache-busts with a "?v=" query string derived from the file's own hash (see
// INKDROP_UI_REACT_VERSION in inkdrop_web_config.py).
//
// The ENTRY keeps that fixed name, because the served document names it in a
// <script> tag. The CHUNKS carry a content hash instead, which is what makes
// them safe to serve immutable without a manifest: a chunk's name changes when
// its content does, so a browser can never be handed stale code under a name
// it already cached. The Python side allowlists whatever `inkdrop-react*.js`
// files the build produced, at import time, so it is still a fixed set checked
// by exact name rather than an open directory read.
//
// Why split at all, measured on this bundle: everything was one 356.24 kB
// (104.14 kB gzip at level 9) file that every page paid for. Opening one
// section now fetches the entry, the React vendor chunk and that section --
// 66.45 kB gzip for Blocklist, 66.03 kB for History -- and the biggest section
// (Series, which carries react-virtuoso) is only paid for by someone who opens
// Series, at 80.82 kB.
export default defineConfig({
  plugins: [react()],
  root: fileURLToPath(new URL(".", import.meta.url)),
  // Dynamic-import URLs are written into the bundle at build time, so they have
  // to be absolute against the path the server actually serves them from.
  base: "/static/dist/",
  build: {
    outDir: fileURLToPath(new URL("../static/dist", import.meta.url)),
    emptyOutDir: true,
    assetsDir: ".",
    rollupOptions: {
      input: fileURLToPath(new URL("src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "inkdrop-react.js",
        chunkFileNames: "inkdrop-react-[name]-[hash].js",
        assetFileNames: "inkdrop-react[extname]",
        // React, ReactDOM and the scheduler are what every section needs and
        // no section owns, so they get their own chunk rather than being
        // duplicated into the first section that happens to load.
        //
        // Matched on the package boundary, not as a substring: an earlier
        // "node_modules/react" test also caught react-virtuoso, which only
        // Series uses -- it went into the shared chunk and every page paid
        // 22 kB gzip for a list renderer it does not mount. Package-scoped,
        // virtuoso stays in the Series chunk where it belongs.
        manualChunks(id: string) {
          const normalized = id.split("\\").join("/");
          for (const name of ["react", "react-dom", "scheduler"]) {
            if (normalized.includes(`/node_modules/${name}/`)) return "vendor";
          }
          return undefined;
        },
      },
    },
  },
  server: {
    // Local dev only: proxy API calls to a real InkDrop instance running on
    // its normal port so cookies/CSRF work exactly like production, same
    // origin semantics as the served app.
    proxy: {
      "/api": {
        target: process.env.INKDROP_DEV_API_ORIGIN || "http://127.0.0.1:8796",
        changeOrigin: false,
      },
    },
  },
});
