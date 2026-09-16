import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // The API runs on its own port in development (journeyman support --serve); in production both
  // are served from one origin, so the client always calls /v1 relative paths.
  server: { proxy: { "/v1": "http://127.0.0.1:8787" } },
  test: { environment: "jsdom", globals: true },
});
