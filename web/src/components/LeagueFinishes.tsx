/**
 * How a deck's league entries went, and how they were told apart.
 *
 * MTGO records no drop in any file the app can read, so entries are
 * rebuilt: every five matches, whenever the registered list changes, and
 * wherever the player marks a drop. Grouping five at a time assumes
 * nobody dropped — one unmarked drop shifts every entry after it — so
 * entries are listed with their matches, and marking a drop is one click.
 */
import { useState } from "react"
import type { LeagueEntry, LeagueSummary } from "@/lib/api"
import { cn } from "@/lib/utils"

/** Green at 5-0 fading to red at 0-5, matching the win-rate palette. */
const TONE = [
  "bg-red-500/70",      // 0 wins
  "bg-red-500/60",
  "bg-amber-500/60",
  "bg-emerald-500/50",  // 3-2, the first prize wall
  "bg-emerald-500/70",
  "bg-emerald-400",     // 5-0
]

const STATUS: Record<LeagueEntry["status"], { label: string; tone: string }> = {
  complete: { label: "finished", tone: "text-muted-foreground" },
  dropped: { label: "dropped", tone: "text-amber-400" },
  unfinished: { label: "not finished", tone: "text-muted-foreground" },
  in_progress: { label: "in progress", tone: "text-primary" },
}

function day(ts: number | null): string {
  if (!ts) return "—"
  return new Date(ts * 1000).toLocaleDateString([], { day: "numeric", month: "short" })
}

function clock(ts: number | null): string {
  if (!ts) return ""
  const d = new Date(ts * 1000)
  return `${d.toLocaleDateString([], { weekday: "short" })} ${d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  })}`
}

export function LeagueFinishes({
  leagues,
  onMark,
  marking = null,
}: {
  leagues: LeagueSummary
  /** End an entry on this match, or take that back. */
  onMark?: (matchId: string, ended: boolean) => void
  /** The match whose mark is being saved. */
  marking?: string | null
}) {
  const [open, setOpen] = useState<number | null>(null)
  const max = Math.max(1, ...leagues.ladder.map((r) => r.runs))

  if (leagues.league_matches === 0) {
    return (
      <p className="px-2 py-6 text-center text-xs leading-relaxed text-muted-foreground">
        No league matches with this deck yet. Runs are counted from league
        matches MTGO confirms you registered it for.
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline gap-4">
        <div>
          <div className="font-mono text-2xl font-semibold tabular-nums">
            {leagues.completed_runs}
          </div>
          <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
            finished
          </div>
        </div>
        <div>
          <div className="flex items-baseline gap-1 font-mono text-2xl font-semibold tabular-nums">
            {leagues.trophies}
            <span className="text-base" aria-hidden>
              🏆
            </span>
          </div>
          <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
            trophies
          </div>
        </div>
        {leagues.average_wins != null && (
          <div className="ml-auto text-right">
            <div className="font-mono text-2xl font-semibold tabular-nums">
              {leagues.average_wins.toFixed(2)}
            </div>
            <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
              avg wins
            </div>
          </div>
        )}
      </div>

      <ul className="flex flex-col gap-1">
        {leagues.ladder.map((r) => (
          <li key={r.record} className="flex items-center gap-2">
            <span className="w-8 shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
              {r.record}
            </span>
            <span className="h-4 flex-1 overflow-hidden rounded-sm bg-muted">
              <span
                className={cn("block h-full rounded-sm", TONE[r.wins])}
                style={{ width: `${(r.runs / max) * 100}%` }}
              />
            </span>
            <span
              className={cn(
                "w-5 shrink-0 text-right font-mono text-[11px] tabular-nums",
                r.runs ? "" : "text-muted-foreground/40"
              )}
            >
              {r.runs}
            </span>
          </li>
        ))}
      </ul>

      <div>
        <div className="mb-1 flex items-baseline justify-between">
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
            Entries
          </span>
          {leagues.dropped_runs > 0 && (
            <span className="font-mono text-[10px] text-muted-foreground">
              {leagues.dropped_runs} ended early
            </span>
          )}
        </div>
        <ul className="flex flex-col">
          {leagues.entries.map((e, i) => {
            const isOpen = open === i
            return (
              <li key={`${e.started_at}-${i}`}>
                <button
                  onClick={() => setOpen(isOpen ? null : i)}
                  className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left hover:bg-muted/50"
                >
                  <span className="w-14 shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
                    {day(e.started_at)}
                  </span>
                  <span className="w-9 shrink-0 font-mono text-[12px] tabular-nums">
                    {e.wins}–{e.losses}
                  </span>
                  <span className={cn("min-w-0 flex-1 truncate text-[11px]", STATUS[e.status].tone)}>
                    {STATUS[e.status].label}
                  </span>
                  <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                    {isOpen ? "hide" : `${e.matches}/5`}
                  </span>
                </button>

                {isOpen && (
                  <ul className="mb-1 ml-3 flex flex-col border-l pl-2">
                    {e.played.map((m, j) => (
                      <li key={m.match_id} className="flex items-center gap-2 py-0.5 text-[11px]">
                        <span
                          className={cn(
                            "w-3 shrink-0 font-mono",
                            m.result === "W" ? "text-emerald-400" : "text-red-400"
                          )}
                        >
                          {m.result}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-muted-foreground">
                          {m.opponent_archetype ?? "unknown deck"}
                        </span>
                        <span className="shrink-0 text-muted-foreground/70">
                          {clock(m.played_at)}
                        </span>
                        {/* The fifth match ends an entry on its own. */}
                        {onMark && j < 4 && (
                          <button
                            disabled={marking === m.match_id}
                            onClick={() => onMark(m.match_id, !m.marked)}
                            title={
                              m.marked
                                ? "This entry did not end here"
                                : "You dropped this entry after this match"
                            }
                            className={cn(
                              "shrink-0 rounded border px-1.5 py-px text-[10px] disabled:opacity-50",
                              m.marked
                                ? "border-amber-400/50 text-amber-400"
                                : "text-muted-foreground hover:text-foreground"
                            )}
                          >
                            {m.marked ? "undo drop" : "dropped here"}
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            )
          })}
        </ul>
      </div>

      <p className="text-[10px] leading-relaxed text-muted-foreground">
        MTGO doesn’t record drops anywhere, so entries are split every five
        matches and whenever you change the list. If you dropped one, open it
        and mark the last match you played.
      </p>
    </div>
  )
}
