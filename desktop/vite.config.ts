import path from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import { architectDevServer } from './server/serve';

// `npm run dev` serves the whole app (UI + control server) at http://localhost:5173.
export default defineConfig({
  plugins: [react(), architectDevServer({ root: process.env.ARCHITECT_ROOT || path.resolve(__dirname, '..') })],
  server: { port: 5173, strictPort: true, host: '127.0.0.1' },
  build: { outDir: 'dist', emptyOutDir: true, target: 'es2022' },
});
