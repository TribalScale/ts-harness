import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173 },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'scripts/**/*.test.mjs'],
    env: { VITE_API_URL: 'http://api.test', VITE_AUTH_MODE: 'test' },
    coverage: {
      provider: 'v8',
      // Pages and components hold the UI logic unit tests must cover (the Gherkin suite
      // covers whole journeys in a browser).
      include: ['src/features/**/*.{ts,tsx}', 'src/components/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}'],
      reporter: ['text-summary', 'text'],
      thresholds: { lines: 80, functions: 80, branches: 75, statements: 80 },
    },
  },
});
