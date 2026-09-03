'use strict';

/**
 * Preload bridge.
 *
 * The UI is a normal web app talking to the local API over HTTP, so it
 * needs almost nothing from Electron. Expose only a marker the frontend
 * can use to enable desktop-only affordances (for example, hiding the
 * "open in browser" hint), and keep the rest of the Node surface closed.
 */

const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('metahunter', {
  isDesktop: true,
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
});
