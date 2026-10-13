import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the backend runs separately; the dev server forwards API and WebSocket calls to it,
// so the browser only ever talks to one origin (no CORS). In Docker, nginx does the same job.
const backend = process.env.BACKEND_URL ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": { target: backend, rewrite: (path) => path.replace(/^\/api/, "") },
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
