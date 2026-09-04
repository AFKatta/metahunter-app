/**
 * How a deck's league entries finished.
 *
 * The ladder is fixed at 5-0 through 0-5 and always drawn in full, so
 * the shape stays readable between decks and an empty rung reads as a
 * real zero rather than a missing row. A 5-0 gets a trophy because
 * that is what MTGO gives you for one, and it is the number anyone
 * actually wants to see first.
 */
import type { LeagueSummary } from "@/lib/api"
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

export function LeagueFinishes({ leagues }: { leagues: LeagueSummary }) {
  const max = Math.max(1, ...leagues.ladder.map((r) => r.runs))

  if (leagues.completed_runs === 0 && !leagues.in_progress) {
    return (
      <p className="px-2 py-6 text-center text-xs leading-relaxed text-muted-foreground">
        No completed league runs for this deck yet.
        {leagues.league_matches > 0
          ? ` ${leagues.league_matches} league match${
              leagues.league_matches === 1 ? "" : "es"
            } recorded so far — a run is scored once all five are in.`
          : " Runs are counted from league matches MTGO confirms you registered this deck for."}
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
            runs
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
            {/* The track is always full width, so a short bar reads as a
                small share of the runs rather than a short list. */}
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

      {(leagues.in_progress || leagues.abandoned_runs > 0) && (
        <p className="rounded border border-dashed px-2 py-1.5 text-[11px] text-muted-foreground">
          {leagues.in_progress && (
            <>
              Run in progress: {leagues.in_progress.wins}-
              {leagues.in_progress.losses} after {leagues.in_progress.matches}{" "}
              of 5.
            </>
          )}
          {leagues.abandoned_runs > 0 && (
            <>
              {leagues.in_progress ? " " : ""}
              {leagues.abandoned_runs} earlier run
              {leagues.abandoned_runs === 1 ? "" : "s"} ended short of five
              matches and {leagues.abandoned_runs === 1 ? "is" : "are"} not
              scored.
            </>
          )}
        </p>
      )}
    </div>
  )
}
