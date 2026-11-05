import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Vitest config is colocated with Vite config. `defineConfig` is imported from
// `vitest/config` so the `test` key is typed and accepted. Vitest's own types
// are resolved via that import and the `vitest/globals` entry in tsconfig.
//
// The dev server proxies `/api` to the local FastAPI backend so the SPA can
// call the backend same-origin during development without CORS.
const BACKEND_TARGET = 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: BACKEND_TARGET,
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: true,
  },
});
