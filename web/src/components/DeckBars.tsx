import { Link } from "react-router-dom"
import type { DeckRow } from "@/lib/api"
import { pct, record, winrateColor } from "@/lib/format"
import { cn } from "@/lib/utils"

/** Pick a fill colour for the bar based on win rate. */
function barFill(wr: number | null | undefined): string {
  if (wr == null) return "bg-muted"
  if (wr >= 0.6) return "bg-emerald-500/30 dark:bg-emerald-500/30"
  if (wr >= 0.5) return "bg-sky-500/25 dark:bg-sky-500/25"
  if (wr >= 0.4) return "bg-amber-500/30 dark:bg-amber-500/30"
  return "bg-rose-500/25 dark:bg-rose-500/30"
}

export function DeckBars({
  rows,
  emptyText = "No matches yet.",
  linkBuilder,
  limit = 12,
}: {
  rows: DeckRow[]
  emptyText?: string
  linkBuilder?: (archetype: string) => string
  limit?: number
}) {
  if (!rows.length) {
    return <div className="text-sm text-muted-foreground">{emptyText}</div>
  }
  const visible = rows.slice(0, limit)
  const maxTotal = Math.max(...visible.map((r) => r.total))
  return (
    <div className="flex flex-col gap-1.5">
      {visible.map((r) => {
        const widthPct = maxTotal ? (r.total / maxTotal) * 100 : 0
        const wrColor = winrateColor(r.winrate)
        const NameNode = linkBuilder ? (
          <Link to={linkBuilder(r.archetype)} className="hover:underline">
            {r.archetype}
          </Link>
        ) : (
          <span>{r.archetype}</span>
        )
        return (
          <div
            key={r.archetype}
            className="group relative grid grid-cols-[1fr_auto_auto] items-center gap-3 overflow-hidden rounded-md border bg-card px-3 py-2.5 transition-shadow hover:shadow-sm"
          >
            <div
              aria-hidden
              className={cn(
                "pointer-events-none absolute inset-y-0 left-0 rounded-l-md transition-all",
                barFill(r.winrate)
              )}
              style={{ width: `${widthPct}%` }}
            />
            <div className="relative z-10 min-w-0 truncate font-medium">{NameNode}</div>
            <div className="relative z-10 font-mono text-sm text-muted-foreground tabular-nums">
              {record(r.wins, r.losses)}
            </div>
            <div
              className={cn(
                "relative z-10 w-12 text-right font-mono text-sm font-semibold tabular-nums",
                wrColor
              )}
            >
              {pct(r.winrate)}
            </div>
          </div>
        )
      })}
    </div>
  )
}
