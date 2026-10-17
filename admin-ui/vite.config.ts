/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const gateway = env.VITE_GATEWAY_PROXY_TARGET || "http://localhost:18789";
  const proxy = {
    target: gateway,
    changeOrigin: true,
  };
  return {
    plugins: [react()],
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      clearMocks: true,
    },
    server: {
      port: Number(env.VITE_DEV_PORT || 5173),
      proxy: {
        "/healthz": proxy,
        "/admin": proxy,
        "/session": proxy,
        "/cron": proxy,
        "/workflow": proxy,
      },
    },
    build: {
      outDir: "dist",
      sourcemap: true,
    },
  };
});
