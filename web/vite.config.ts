import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    // Which build wrote a cached response; see src/lib/persist.ts.
    __APP_BUILD__: JSON.stringify(new Date().toISOString()),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    // Forward /api requests to the FastAPI backend on 8765 in dev.
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
})
