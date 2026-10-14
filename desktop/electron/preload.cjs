/**
 * Architect desktop preload: native affordances only. The page feature-detects
 * `window.architectNative`; in a plain browser it is undefined.
 */
const { contextBridge, ipcRenderer, webFrame, webUtils } = require('electron');

contextBridge.exposeInMainWorld('architectNative', {
  pickFolder: (start) => ipcRenderer.invoke('architect:pick-folder', start),
  reveal: (path) => ipcRenderer.invoke('architect:reveal', path),
  openExternal: (url) => ipcRenderer.invoke('architect:open-external', url),
  openPath: (path) => ipcRenderer.invoke('architect:open-path', path),
  /** The real path of a dropped or picked file (Electron 32+ has no File.path). */
  pathForFile: (file) => webUtils.getPathForFile(file),
  setTitleBar: (dark) => ipcRenderer.invoke('architect:title-bar', !!dark),
  platform: process.platform,
  /** Commands from the app menu (macOS menu bar): `cb(command)`. Returns an unsubscribe. */
  onMenu: (cb) => {
    const listener = (_event, command) => cb(command);
    ipcRenderer.on('architect:menu', listener);
    return () => ipcRenderer.removeListener('architect:menu', listener);
  },
  /** Full screen hides the traffic lights, so the header can take their space back. */
  onFullScreen: (cb) => {
    const listener = (_event, on) => cb(!!on);
    ipcRenderer.on('architect:full-screen', listener);
    return () => ipcRenderer.removeListener('architect:full-screen', listener);
  },
  version: () => ipcRenderer.invoke('architect:version'),
  /** Another window: '' for the whole app, 'chat=<id>' or 'pane=<tab>&conv=<id>' for a pop-out. */
  openWindow: (route, options) => ipcRenderer.invoke('architect:open-window', String(route), options || {}),
  setAlwaysOnTop: (on) => ipcRenderer.invoke('architect:always-on-top', !!on),
  /** Links a Browser page opens in a new window: `cb(url)`, shown as a new tab. */
  onBrowserOpen: (cb) => {
    const listener = (_event, url) => cb(String(url));
    ipcRenderer.on('architect:browser-open', listener);
    return () => ipcRenderer.removeListener('architect:browser-open', listener);
  },
  /** Keep the computer from sleeping while a turn runs. */
  keepAwake: (on) => ipcRenderer.invoke('architect:keep-awake', !!on),
  toggleFullScreen: () => ipcRenderer.invoke('architect:toggle-full-screen'),
  /** Page zoom in steps (0 is 100%); `null` resets. Returns the new level. */
  zoom: (delta) => {
    const level = delta === null ? 0 : Math.max(-3, Math.min(5, webFrame.getZoomLevel() + delta));
    webFrame.setZoomLevel(level);
    return level;
  },
});
