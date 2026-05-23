import { useQuery } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import { api } from "@/lib/api"
import { ago } from "@/lib/format"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { ScrollArea } from "@/components/ui/scroll-area"

export function MatchDetail() {
  const { id } = useParams<{ id: string }>()
  const q = useQuery({
    queryKey: ["match", id],
    queryFn: () => api.match(id!),
    enabled: !!id,
  })
  const logQ = useQuery({
    queryKey: ["match-log", id],
    queryFn: () => api.matchLog(id!),
    enabled: !!id,
    // The log file is static once written — no need to refetch.
    staleTime: Infinity,
  })

  if (q.isLoading) {
    return <div className="mx-auto max-w-7xl px-6 py-6 text-sm text-muted-foreground">Loading…</div>
  }
  if (q.isError || !q.data) {
    return (
      <div className="mx-auto max-w-7xl px-6 py-6 text-sm text-rose-500">
        Match not found.
      </div>
    )
  }
  const m = q.data
  const youOrFirst = m.user ?? m.players[0]
  const opp = m.players.find((p) => p !== youOrFirst) ?? m.players[1] ?? ""

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {youOrFirst} vs {opp}
          </h1>
          <p className="text-sm text-muted-foreground">
            {ago(m.log_mtime)} · {m.turns} turns
            {m.match_winner && (
              <> · winner <span className="font-medium">{m.match_winner}</span>{" "}
                {m.score && <>({m.score[0]}–{m.score[1]})</>}</>
            )}
          </p>
        </div>
        <Link
          to={`/matches?opponent=${encodeURIComponent(opp)}`}
          className="text-sm text-muted-foreground underline-offset-4 hover:underline"
        >
          More vs {opp} →
        </Link>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        {m.players.map((p) => {
          const isYou = p === m.user
          const sigs = m.signatures[p] ?? []
          const arch = m.archetypes[p] ?? "Unknown"
          return (
            <Card key={p}>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center gap-2">
                  {p}
                  {isYou && <Badge variant="secondary">you</Badge>}
                </CardTitle>
                <CardDescription>
                  Classified as <span className="font-medium text-foreground">{arch}</span>
                </CardDescription>
              </CardHeader>
              <CardContent>
                <ScrollArea className="h-[340px] pr-3">
                  <ul className="flex flex-col gap-0.5 text-sm">
                    {sigs.map((s) => (
                      <li
                        key={s.name}
                        className="flex items-center justify-between rounded px-2 py-1 hover:bg-accent"
                      >
                        <span className="truncate">{s.name}</span>
                        <span className="ml-2 font-mono tabular-nums text-muted-foreground">
                          ×{s.count}
                        </span>
                      </li>
                    ))}
                    {sigs.length === 0 && (
                      <li className="text-muted-foreground">No cards observed.</li>
                    )}
                  </ul>
                </ScrollArea>
              </CardContent>
            </Card>
          )
        })}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Game outcomes</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="flex flex-col gap-2 text-sm">
            {m.games.map((g, i) => (
              <li key={i} className="flex items-center gap-3">
                <Badge variant="outline">Game {i + 1}</Badge>
                <span>
                  <span className="font-medium">{g.winner ?? "?"}</span>{" "}
                  beat <span className="font-medium">{g.loser ?? "?"}</span>
                  {g.by_concede && <span className="text-muted-foreground"> · concede</span>}
                </span>
              </li>
            ))}
            {m.games.length === 0 && (
              <li className="text-muted-foreground">No per-game outcome recorded.</li>
            )}
          </ol>
          <Separator className="my-4" />
          <div className="text-xs text-muted-foreground">
            Log file: <code className="rounded bg-muted px-1.5 py-0.5">{m.log_path}</code>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2">
            Log text
            {logQ.data?.truncated && (
              <Badge variant="outline" className="text-xs">truncated</Badge>
            )}
          </CardTitle>
          <CardDescription>
            {logQ.isLoading
              ? "Reading log…"
              : logQ.isError || !logQ.data
              ? "Log file unavailable."
              : `${logQ.data.size_bytes.toLocaleString()} bytes from the MTGO Match_GameLog`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {logQ.data?.text && (
            <ScrollArea className="h-[480px] rounded border bg-muted/30">
              <pre className="whitespace-pre-wrap break-all p-3 font-mono text-xs leading-relaxed">
                {logQ.data.text}
              </pre>
            </ScrollArea>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
