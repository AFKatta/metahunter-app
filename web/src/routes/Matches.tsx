import { useMemo, useState } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { api } from "@/lib/api"
import { usePersistedQuery } from "@/lib/persist"
import { ago } from "@/lib/format"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import {
  DEFAULT_RANGE,
  Timeline,
  rangeToParams,
  type RangeMode,
} from "@/components/Timeline"
import { cn } from "@/lib/utils"
import { useFormat } from "@/components/FormatProvider"
import { useAccount } from "@/components/AccountProvider"

function rangeKey(r: RangeMode): string {
  if (r.kind === "preset") return `preset:${r.days}`
  return `range:${r.from}-${r.to}`
}

export function Matches() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const yourDeck = params.get("your_deck") ?? ""
  const theirDeck = params.get("their_deck") ?? ""
  const opponent = params.get("opponent") ?? ""
  const page = Math.max(1, Number(params.get("page") ?? "1"))

  const [range, setRange] = useState<RangeMode>(DEFAULT_RANGE)
  const { format } = useFormat()
  const { account } = useAccount()
  const rangeParams = { ...rangeToParams(range), format, user: account || undefined }
  const rkey = `${rangeKey(range)}|${format}|${account}`

  const setParam = (k: string, v: string | null) => {
    const next = new URLSearchParams(params)
    if (v == null || v === "") next.delete(k)
    else next.set(k, v)
    if (k !== "page") next.delete("page")
    setParams(next, { replace: true })
  }

  // Persisted: reopening the app should show the last page of
  // matches immediately and correct it behind you.
  const q = usePersistedQuery({
    queryKey: ["matches", rkey, yourDeck, theirDeck, opponent, page],
    queryFn: () =>
      api.matches({
        ...rangeParams,
        your_deck: yourDeck || undefined,
        their_deck: theirDeck || undefined,
        opponent: opponent || undefined,
        page,
        page_size: 50,
      }),
  })

  const rows = q.data?.items ?? []
  const total = q.data?.total ?? 0
  const pageSize = q.data?.page_size ?? 50
  const lastPage = Math.max(1, Math.ceil(total / pageSize))

  const filtersActive = !!yourDeck || !!theirDeck || !!opponent

  const ResultBadge = useMemo(
    () =>
      function R({ result }: { result: "W" | "L" | "?" }) {
        const cls =
          result === "W"
            ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 dark:bg-emerald-500/15"
            : result === "L"
            ? "bg-rose-500/20 text-rose-700 dark:text-rose-300 dark:bg-rose-500/15"
            : "bg-muted text-muted-foreground"
        return (
          <Badge className={cn("border-transparent font-semibold", cls)} variant="outline">
            {result}
          </Badge>
        )
      },
    []
  )

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-6">
      <div className="flex flex-wrap items-center gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Matches</h1>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Input
            placeholder="Your deck…"
            className="w-[180px]"
            value={yourDeck}
            onChange={(e) => setParam("your_deck", e.target.value)}
          />
          <Input
            placeholder="Their deck…"
            className="w-[180px]"
            value={theirDeck}
            onChange={(e) => setParam("their_deck", e.target.value)}
          />
          <Input
            placeholder="Opponent name…"
            className="w-[160px]"
            value={opponent}
            onChange={(e) => setParam("opponent", e.target.value)}
          />
          {filtersActive && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setParams({}, { replace: true })}
            >
              Reset
            </Button>
          )}
        </div>
      </div>

      <Timeline value={range} onChange={setRange} />

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            {q.isLoading ? "Loading…" : `${total.toLocaleString()} matches`}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[120px]">When</TableHead>
                  <TableHead>Opponent</TableHead>
                  <TableHead>Your deck</TableHead>
                  <TableHead>Their deck</TableHead>
                  <TableHead className="w-[80px]">Result</TableHead>
                  <TableHead className="w-[70px] text-center">Play</TableHead>
                  <TableHead className="w-[80px] text-right">Score</TableHead>
                  <TableHead className="w-[70px] text-right">Turns</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => {
                  // Open the match detail page on a click anywhere in
                  // the row. Ctrl/⌘/middle-click opens in a new tab.
                  const open = (
                    e: React.MouseEvent<HTMLTableRowElement>
                  ) => {
                    const newTab =
                      e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1
                    const url = `/match/${r.match_id}`
                    if (newTab) {
                      window.open(url, "_blank", "noopener")
                    } else {
                      navigate(url)
                    }
                  }
                  return (
                    <TableRow
                      key={r.match_id}
                      onClick={open}
                      onAuxClick={open}
                      className="cursor-pointer hover:bg-accent/40"
                    >
                      <TableCell className="text-muted-foreground">
                        {ago(r.log_mtime)}
                      </TableCell>
                      <TableCell className="font-medium">{r.opponent}</TableCell>
                      <TableCell>{r.your_deck}</TableCell>
                      <TableCell>{r.their_deck}</TableCell>
                      <TableCell>
                        <ResultBadge result={r.result} />
                      </TableCell>
                      <TableCell className="text-center">
                        {r.on_play == null ? (
                          <span className="text-muted-foreground">–</span>
                        ) : r.on_play ? (
                          <span
                            title="On the play (you went first in game 1)"
                            className="rounded bg-sky-500/15 px-2 py-0.5 text-xs font-semibold text-sky-700 dark:text-sky-300"
                          >
                            P
                          </span>
                        ) : (
                          <span
                            title="On the draw (opponent went first in game 1)"
                            className="rounded bg-amber-500/15 px-2 py-0.5 text-xs font-semibold text-amber-700 dark:text-amber-300"
                          >
                            D
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {r.your_games}–{r.their_games}
                      </TableCell>
                      <TableCell className="text-right font-mono text-muted-foreground tabular-nums">
                        {r.turns}
                      </TableCell>
                    </TableRow>
                  )
                })}
                {rows.length === 0 && !q.isLoading && (
                  <TableRow>
                    <TableCell colSpan={8} className="py-12 text-center text-muted-foreground">
                      No matches.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
          <div className="mt-3 flex items-center justify-between text-sm">
            <div className="text-muted-foreground">
              Page {page} of {lastPage}
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page <= 1}
                onClick={() => setParam("page", String(page - 1))}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= lastPage}
                onClick={() => setParam("page", String(page + 1))}
              >
                Next
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
