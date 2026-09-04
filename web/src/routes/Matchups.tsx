import { useMemo, useState } from "react"
import { Link } from "react-router-dom"
import { api } from "@/lib/api"
import { usePersistedQuery } from "@/lib/persist"
import { pct, winrateColor } from "@/lib/format"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import {
  DEFAULT_RANGE,
  Timeline,
  rangeToParams,
  type RangeMode,
} from "@/components/Timeline"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"
import { useFormat } from "@/components/FormatProvider"
import { useAccount } from "@/components/AccountProvider"

function rangeKey(r: RangeMode): string {
  if (r.kind === "preset") return `preset:${r.days}`
  return `range:${r.from}-${r.to}`
}

export function Matchups() {
  const [range, setRange] = useState<RangeMode>(DEFAULT_RANGE)
  const [yourDeck, setYourDeck] = useState<string>("")
  const { format } = useFormat()
  const { account } = useAccount()
  const rangeParams = { ...rangeToParams(range), format, user: account || undefined }
  const rkey = `${rangeKey(range)}|${format}|${account}`

  const decks = usePersistedQuery({ queryKey: ["decks", rkey], queryFn: () => api.decks(rangeParams) })
  // The matrix is the slowest thing in the app to compute, and it
  // barely moves between launches — exactly what a cache is for.
  const matchups = usePersistedQuery({
    queryKey: ["matchups", rkey, yourDeck],
    queryFn: () => api.matchups({ ...rangeParams, your_deck: yourDeck || undefined }),
  })

  const matrix = useMemo(() => {
    if (yourDeck) return null
    const ys = new Set<string>()
    const os = new Set<string>()
    const cell: Record<string, Record<string, { w: number; l: number; t: number }>> = {}
    for (const r of matchups.data ?? []) {
      ys.add(r.your_deck)
      os.add(r.their_deck)
      ;(cell[r.your_deck] ??= {})[r.their_deck] = { w: r.wins, l: r.losses, t: r.total }
    }
    const yArr = Array.from(ys).sort((a, b) => {
      const at = (matchups.data ?? []).filter((r) => r.your_deck === a).reduce((s, r) => s + r.total, 0)
      const bt = (matchups.data ?? []).filter((r) => r.your_deck === b).reduce((s, r) => s + r.total, 0)
      return bt - at
    })
    const oArr = Array.from(os).sort((a, b) => {
      const at = (matchups.data ?? []).filter((r) => r.their_deck === a).reduce((s, r) => s + r.total, 0)
      const bt = (matchups.data ?? []).filter((r) => r.their_deck === b).reduce((s, r) => s + r.total, 0)
      return bt - at
    })
    return { ys: yArr, os: oArr, cell }
  }, [matchups.data, yourDeck])

  const linear = matchups.data ?? []

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-6">
      <div className="flex flex-wrap items-center gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Matchups</h1>
          <p className="text-sm text-muted-foreground">
            Cell colour shows win rate. Hover for absolute record.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Select value={yourDeck || "__all"} onValueChange={(v) => setYourDeck(!v || v === "__all" ? "" : v)}>
            <SelectTrigger className="w-[220px]">
              <SelectValue placeholder="As all decks…" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">All your decks (matrix)</SelectItem>
              {(decks.data ?? []).map((d) => (
                <SelectItem key={d.archetype} value={d.archetype}>
                  {d.archetype} ({d.total})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <Timeline value={range} onChange={setRange} />

      {yourDeck ? (
        <Card>
          <CardHeader>
            <CardTitle>As {yourDeck}</CardTitle>
            <CardDescription>
              {linear.reduce((s, r) => s + r.total, 0)} decided matches against{" "}
              {linear.length} archetypes.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="grid gap-2 sm:grid-cols-2">
              {linear.map((r) => (
                <li key={`${r.your_deck}-${r.their_deck}`} className="flex items-center justify-between rounded-md border bg-card px-3 py-2 text-sm">
                  <Link
                    to={`/matches?your_deck=${encodeURIComponent(r.your_deck)}&their_deck=${encodeURIComponent(r.their_deck)}`}
                    className="truncate hover:underline"
                  >
                    vs {r.their_deck}
                  </Link>
                  <div className="ml-3 flex items-center gap-3 font-mono tabular-nums">
                    <span className="text-muted-foreground">{r.wins}–{r.losses}</span>
                    <span className={cn("w-12 text-right", winrateColor(r.winrate))}>{pct(r.winrate)}</span>
                  </div>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : matrix && matrix.ys.length > 0 ? (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="min-w-full border-separate border-spacing-0 text-xs">
                <thead className="bg-muted/40">
                  <tr>
                    <th className="sticky left-0 z-10 bg-muted/40 px-3 py-2 text-left font-medium">
                      Your deck ↓ / vs →
                    </th>
                    {matrix.os.map((o) => (
                      <th key={o} className="whitespace-nowrap px-2 py-2 text-left font-medium" title={o}>
                        <div className="max-w-[120px] truncate">{o}</div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {matrix.ys.map((y) => (
                    <tr key={y} className="border-t">
                      <th className="sticky left-0 z-10 bg-background px-3 py-2 text-left font-medium whitespace-nowrap">
                        {y}
                      </th>
                      {matrix.os.map((o) => {
                        const c = matrix.cell[y]?.[o]
                        if (!c) {
                          return <td key={o} className="border-l px-2 py-2 text-center text-muted-foreground/30">·</td>
                        }
                        const wr = c.t ? c.w / c.t : null
                        const bg =
                          wr == null
                            ? ""
                            : wr >= 0.6
                              ? "bg-emerald-500/30"
                              : wr >= 0.5
                                ? "bg-sky-500/20"
                                : wr >= 0.4
                                  ? "bg-amber-500/25"
                                  : "bg-rose-500/30"
                        return (
                          <td
                            key={o}
                            className={cn("border-l px-2 py-2 text-center font-mono tabular-nums", bg)}
                            title={`${y} vs ${o}: ${c.w}–${c.l}`}
                          >
                            <Link
                              to={`/matches?your_deck=${encodeURIComponent(y)}&their_deck=${encodeURIComponent(o)}`}
                              className="block hover:underline"
                            >
                              <div className={winrateColor(wr)}>{pct(wr)}</div>
                              <div className="text-[10px] text-muted-foreground">{c.w}–{c.l}</div>
                            </Link>
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      ) : (
        <div className="text-sm text-muted-foreground">No matchup data yet.</div>
      )}
    </div>
  )
}
