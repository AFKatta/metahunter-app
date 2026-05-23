import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { api } from "@/lib/api"
import { ago, pct, record, winrateColor } from "@/lib/format"
import { HeroStat, pickWinRateTone } from "@/components/HeroStat"
import { cn } from "@/lib/utils"
import { DeckBars } from "@/components/DeckBars"
import { useFormat } from "@/components/FormatProvider"
import { useAccount } from "@/components/AccountProvider"
import {
  DEFAULT_RANGE,
  Timeline,
  rangeToParams,
  type RangeMode,
} from "@/components/Timeline"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

function rangeKey(r: RangeMode): string {
  if (r.kind === "preset") return `preset:${r.days}`
  return `range:${r.from}-${r.to}`
}

export function Overview() {
  const [range, setRange] = useState<RangeMode>(DEFAULT_RANGE)
  const { format } = useFormat()
  const { account } = useAccount()
  const params = { ...rangeToParams(range), format, user: account || undefined }
  const key = `${rangeKey(range)}|${format}|${account}`

  const me = useQuery({ queryKey: ["me", key], queryFn: () => api.me(params) })
  const decks = useQuery({ queryKey: ["decks", key], queryFn: () => api.decks(params) })
  const opps = useQuery({ queryKey: ["opponents", key], queryFn: () => api.opponents(params) })

  const matchWR = me.data?.match_winrate ?? null
  const gameWR = me.data?.game_winrate ?? null

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">
            {me.data?.user ? (
              <>Hello, <span className="bg-gradient-to-r from-primary to-sky-500 bg-clip-text text-transparent">{me.data.user}</span></>
            ) : (
              "Overview"
            )}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Last match {ago(me.data?.last_match_at)}.{" "}
            {me.data?.total_matches ? `${me.data.total_matches} matches in window.` : null}
          </p>
        </div>
      </div>

      <Timeline value={range} onChange={setRange} />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <HeroStat
          label="Match win rate"
          value={pct(matchWR, 1)}
          tone={pickWinRateTone(matchWR)}
          hint={
            me.data?.decided_matches
              ? `${record(me.data.match_wins, me.data.match_losses)} in ${me.data.decided_matches} decided matches`
              : "—"
          }
        />
        <HeroStat
          label="Game win rate"
          value={pct(gameWR, 1)}
          tone={pickWinRateTone(gameWR)}
          hint={
            me.data && (me.data.game_wins + me.data.game_losses)
              ? `${record(me.data.game_wins, me.data.game_losses)} games`
              : "—"
          }
        />
        <HeroStat
          label="Matches played"
          value={me.data?.total_matches ?? "–"}
          tone="primary"
          hint={
            me.data?.first_match_at
              ? `Since ${new Date(me.data.first_match_at * 1000).toLocaleDateString()}`
              : "—"
          }
        />
        <HeroStat
          label="Archetypes faced"
          value={opps.data?.length ?? "–"}
          tone="sky"
          hint={`${decks.data?.length ?? 0} different decks of yours`}
        />
      </div>

      {/* Secondary metric strip: on-the-play vs on-the-draw winrates.
          Hidden when neither side has data, e.g. on a fresh DB. */}
      {me.data && (me.data.play_wins + me.data.play_losses +
                   me.data.draw_wins + me.data.draw_losses > 0) && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              On the play vs on the draw
            </CardTitle>
            <CardDescription>Decided by who chose to play first in game 1.</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-6">
              <div className="flex items-baseline gap-3">
                <span className="rounded bg-sky-500/15 px-2 py-0.5 text-xs font-semibold text-sky-700 dark:text-sky-300">
                  ON THE PLAY
                </span>
                <span className={cn("text-2xl font-semibold tabular-nums",
                                    winrateColor(me.data.play_winrate))}>
                  {pct(me.data.play_winrate, 1)}
                </span>
                <span className="text-sm text-muted-foreground tabular-nums">
                  {record(me.data.play_wins, me.data.play_losses)}
                </span>
              </div>
              <div className="flex items-baseline gap-3">
                <span className="rounded bg-amber-500/15 px-2 py-0.5 text-xs font-semibold text-amber-700 dark:text-amber-300">
                  ON THE DRAW
                </span>
                <span className={cn("text-2xl font-semibold tabular-nums",
                                    winrateColor(me.data.draw_winrate))}>
                  {pct(me.data.draw_winrate, 1)}
                </span>
                <span className="text-sm text-muted-foreground tabular-nums">
                  {record(me.data.draw_wins, me.data.draw_losses)}
                </span>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" />
              Your decks
            </CardTitle>
            <CardDescription>Classified from cards you cast in each match.</CardDescription>
          </CardHeader>
          <CardContent>
            <DeckBars
              rows={decks.data ?? []}
              emptyText={decks.isLoading ? "Loading…" : "No matches in window."}
              linkBuilder={(a) => `/matches?your_deck=${encodeURIComponent(a)}`}
            />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <span className="inline-block h-2 w-2 rounded-full bg-rose-500" />
              Opponent archetypes
            </CardTitle>
            <CardDescription>What you've faced, and how you've fared.</CardDescription>
          </CardHeader>
          <CardContent>
            <DeckBars
              rows={opps.data ?? []}
              emptyText={opps.isLoading ? "Loading…" : "No matches in window."}
              linkBuilder={(a) => `/matches?their_deck=${encodeURIComponent(a)}`}
            />
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
