import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build lands inside the Python package so `uvicorn app.api.main:app` still serves
// the whole demo from one command. `ui.html` stays as the no-build fallback: if this
// directory has never been built, the API serves that instead of a blank page.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../app/api/web", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/batches": "http://127.0.0.1:8010",
      "/policy": "http://127.0.0.1:8010",
      "/live": "http://127.0.0.1:8010",
    },
  },
});
