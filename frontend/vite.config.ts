import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Tailwind v4 is configured through the Vite plugin + a CSS-first `@theme`
// block in src/styles/index.css — there is no tailwind.config.js.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  // `preview` serves the production build locally. It does NOT inherit the dev
  // server's proxy, so /api is declared for both — otherwise `npm run preview`
  // renders the shell and then fails every request.
  preview: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_BASE_URL ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  server: {
    port: 5173,
    // The browser talks to /api on the same origin in dev; Vite forwards to
    // FastAPI. This keeps the production and development URL shapes identical.
    proxy: {
      "/api": {
        target: process.env.VITE_API_BASE_URL ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    // Plotly is dynamically imported in ChartView and lands in its own chunk,
    // so it is fetched only when a chart first renders. The limit is raised
    // because that chunk is legitimately large, not because the warning is
    // being ignored — the app shell itself stays small.
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
        },
      },
    },
  },
});
