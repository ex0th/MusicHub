// @ts-check
import { defineConfig } from "astro/config";

// Statischer Build: Das Frontend wird von nginx ausgeliefert, nginx leitet /api an das
// Backend weiter. Im Dev-Server übernimmt Vite diese Weiterleitung.
const apiTarget = process.env.MUSICHUB_API_URL ?? "http://localhost:8000";

export default defineConfig({
  output: "static",
  devToolbar: { enabled: false },
  vite: {
    server: {
      proxy: {
        "/api": { target: apiTarget, changeOrigin: false },
      },
    },
  },
});
