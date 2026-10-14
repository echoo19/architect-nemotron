/** The loopback server Electron loads the app from (and the dev-mode Vite plugin). */
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import type { Plugin } from 'vite';
import { Hub, type HubOptions } from './hub.js';

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.json': 'application/json',
  '.ico': 'image/x-icon',
};

export interface Running {
  port: number;
  url: string;
  hub: Hub;
  close: () => Promise<void>;
}

export function startServer(uiRoot: string, options: HubOptions, port = 0): Promise<Running> {
  const hub = new Hub(options);
  hub.routines.startTimer();
  const server = http.createServer((req, res) => {
    void hub.handleHttp(req, res).then((handled) => {
      if (handled) return;
      const url = new URL(req.url ?? '/', 'http://localhost');
      const rel = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname.slice(1));
      const filePath = path.join(uiRoot, path.normalize(rel));
      const safe = filePath.startsWith(uiRoot) && fs.existsSync(filePath) && fs.statSync(filePath).isFile();
      const finalPath = safe ? filePath : path.join(uiRoot, 'index.html');
      res.statusCode = 200;
      res.setHeader('Content-Type', MIME[path.extname(finalPath)] ?? 'application/octet-stream');
      fs.createReadStream(finalPath).pipe(res);
    });
  });
  hub.attach(server);
  return new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const address = server.address();
      const actual = typeof address === 'object' && address ? address.port : port;
      resolve({
        port: actual,
        url: `http://127.0.0.1:${actual}`,
        hub,
        close: async () => {
          await hub.closeAll();
          await new Promise<void>((done) => server.close(() => done()));
        },
      });
    });
  });
}

/** Dev mode: the same hub inside Vite's server, so http://localhost:5173 is the whole app. */
export function architectDevServer(options: HubOptions): Plugin {
  return {
    name: 'architect-desktop-server',
    configureServer(server) {
      const hub = new Hub(options);
      hub.routines.startTimer();
      if (server.httpServer) hub.attach(server.httpServer as http.Server);
      server.middlewares.use((req, res, next) => {
        void hub.handleHttp(req, res).then((handled) => {
          if (!handled) next();
        });
      });
      server.httpServer?.on('close', () => void hub.closeAll());
    },
  };
}
