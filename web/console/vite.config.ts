import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发代理到本地后端；生产由 nginx 托管 dist（web/Dockerfile）
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
