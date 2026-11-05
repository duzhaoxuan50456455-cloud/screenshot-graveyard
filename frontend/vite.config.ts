import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Vitest config is colocated with Vite config. `defineConfig` is imported from
// `vitest/config` so the `test` key is typed and accepted. Vitest's own types
// are resolved via that import and the `vitest/globals` entry in tsconfig.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: true,
  },
});
