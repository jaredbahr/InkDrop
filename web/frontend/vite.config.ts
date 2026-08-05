import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// InkDrop's server (inkdrop_web.py) is a hand-rolled http.server app, not a
// framework with a manifest reader. It serves static JS/CSS by fixed path and
// cache-busts with a manually bumped "?v=" query string (see
// INKDROP_UI_JS_VERSION in inkdrop_web.py), the same convention every other
// static asset in this app already uses. Emitting hashed/chunked filenames
// here would require teaching the Python server to parse a Vite manifest for
// no real benefit at this app's size, so output is pinned to fixed names and
// inlined into a single file instead.
export default defineConfig({
  plugins: [react()],
  root: fileURLToPath(new URL(".", import.meta.url)),
  build: {
    outDir: fileURLToPath(new URL("../static/dist", import.meta.url)),
    emptyOutDir: true,
    assetsDir: ".",
    codeSplitting: false,
    rollupOptions: {
      input: fileURLToPath(new URL("src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "inkdrop-react.js",
        chunkFileNames: "inkdrop-react.js",
        assetFileNames: "inkdrop-react[extname]",
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
