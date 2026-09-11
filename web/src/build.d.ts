/**
 * Set at build time in vite.config.ts. Identifies the build that wrote a
 * cached response, so one release never paints another release's data.
 */
declare const __APP_BUILD__: string
