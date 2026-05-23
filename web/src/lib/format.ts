export const pct = (n: number | null | undefined, digits = 0) =>
  n == null ? "–" : `${(n * 100).toFixed(digits)}%`

export const record = (w: number, l: number) => `${w}–${l}`

export const ago = (epochSeconds: number | null | undefined) => {
  if (!epochSeconds) return "–"
  const delta = Date.now() / 1000 - epochSeconds
  if (delta < 60) return "just now"
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  if (delta < 86400 * 30) return `${Math.floor(delta / 86400)}d ago`
  return new Date(epochSeconds * 1000).toLocaleDateString()
}

export const winrateColor = (winrate: number | null | undefined) => {
  if (winrate == null) return "text-muted-foreground"
  if (winrate >= 0.6) return "text-emerald-600 dark:text-emerald-400"
  if (winrate >= 0.5) return "text-foreground"
  if (winrate >= 0.4) return "text-amber-600 dark:text-amber-400"
  return "text-rose-600 dark:text-rose-400"
}
