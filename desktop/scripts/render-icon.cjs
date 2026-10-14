// Render build/icon.svg to build/icon.png (1024x1024) with Electron itself:
//   npx electron scripts/render-icon.cjs
// electron-builder makes the .icns and .ico from the PNG.
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  const svg = fs.readFileSync(path.join(__dirname, '..', 'build', 'icon.svg'), 'utf8');
  const win = new BrowserWindow({ width: 1024, height: 1024, show: false, frame: false, transparent: true, useContentSize: true, webPreferences: { offscreen: true } });
  win.webContents.setZoomFactor(1);
  const html = `<html><body style="margin:0;background:transparent">${svg.replace('<svg ', '<svg width="1024" height="1024" ')}</body></html>`;
  await win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  await new Promise((r) => setTimeout(r, 400));
  const image = await win.webContents.capturePage({ x: 0, y: 0, width: 1024, height: 1024 });
  const out = image.getSize().width === 1024 ? image : image.resize({ width: 1024, height: 1024, quality: 'best' });
  fs.writeFileSync(path.join(__dirname, '..', 'build', 'icon.png'), out.toPNG());
  console.log('build/icon.png', out.getSize());
  app.exit(0);
});
