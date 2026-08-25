import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Собранный фронтенд кладём в ../static — оттуда его отдаёт FastAPI,
// поэтому отдельный веб-сервер в контейнере не нужен.
export default defineConfig({
  base: "/static/",
  plugins: [react(), tailwindcss()],
  build: { outDir: "../static", emptyOutDir: true },
  server: {
    // Для разработки: vite отдаёт интерфейс, API берём у локального шлюза.
    proxy: { "/api": "http://127.0.0.1:8799" },
  },
});
