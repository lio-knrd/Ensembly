import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend port (matches APP_PORT in .env; default 8420).
const BACKEND = "http://localhost:8420";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/media": { target: BACKEND, changeOrigin: true },
      "/ws": { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
});
