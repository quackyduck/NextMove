import { defineConfig } from "vite";
export default defineConfig({
  base: process.env.NEXT_MOVE_BASE_PATH || "/",
  server: {
    proxy: { "/api": process.env.API_PROXY_TARGET || "http://127.0.0.1:5001" },
  },
});
