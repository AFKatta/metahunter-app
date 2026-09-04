import { useEffect, useMemo, useState } from "react"
import {
  Area,
  Bar,
  Brush,
  CartesianGrid,
  ComposedChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { api, type RangeParams, type TimelineRow } from "@/lib/api"
import { usePersistedQuery } from "@/lib/persist"
import { pct } from "@/lib/format"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { useFormat } from "@/components/FormatProvider"
import { useAccount } from "@/components/AccountProvider"

/** A bounded date range. Both inclusive; expressed as epoch-seconds at
 *  UTC midnight (matching the `ts` on each timeline row). `null` ends
 *  mean "open" on that side. */
export type Range = { from: number | null; to: number | null }

export type RangeMode =
  | { kind: "preset"; days: number | "all" }
  | { kind: "range"; from: number; to: number }

const PRESETS: { label: string; days: number | "all" }[] = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
  { label: "365d", days: 365 },
  { label: "All", days: "all" },
]

function fmt(d: string) {
  const [, mo, da] = d.split("-")
  return `${mo}/${da}`
}

function fmtFull(d: string) {
  const dt = new Date(d + "T00:00:00Z")
  return dt.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
}

export function Timeline({
  value,
  onChange,
  className,
}: {
  value: RangeMode
  onChange: (v: RangeMode) => void
  className?: string
}) {
  // Always fetch the FULL history for the timeline regardless of the
  // current window, so the chart context doesn't collapse when you zoom in.
  // Scoped to the current format + account so each combination has
  // its own timeline.
  const { format } = useFormat()
  const { account } = useAccount()
  const q = usePersistedQuery({
    queryKey: ["timeline-all", format, account],
    queryFn: () => api.timeline({ format, user: account || undefined }),
  })

  const rows = q.data ?? []
  const total = useMemo(
    () => rows.reduce((s, r) => s + r.matches, 0),
    [rows]
  )

  // Resolve the active window to [startIndex, endIndex] in `rows`.
  const [startIdx, endIdx] = useMemo<[number, number]>(() => {
    if (!rows.length) return [0, 0]
    if (value.kind === "preset") {
      if (value.days === "all") return [0, rows.length - 1]
      const end = rows.length - 1
      const start = Math.max(0, rows.length - Number(value.days))
      return [start, end]
    }
    const start = rows.findIndex((r) => r.ts >= value.from)
    const endRev = [...rows].reverse().findIndex((r) => r.ts <= value.to)
    return [
      start < 0 ? 0 : start,
      endRev < 0 ? rows.length - 1 : rows.length - 1 - endRev,
    ]
  }, [rows, value])

  // Local mirror so Recharts' Brush isn't forced to re-render on every parent update.
  const [brush, setBrush] = useState<[number, number]>([startIdx, endIdx])
  useEffect(() => setBrush([startIdx, endIdx]), [startIdx, endIdx])

  const onBrushChange = (e?: { startIndex?: number; endIndex?: number }) => {
    if (!e) return
    const s = e.startIndex ?? 0
    const en = e.endIndex ?? rows.length - 1
    setBrush([s, en])
    if (!rows.length) return
    const fromRow = rows[s]
    const toRow = rows[en]
    if (!fromRow || !toRow) return
    onChange({ kind: "range", from: fromRow.ts, to: toRow.ts })
  }

  const decided = rows.slice(startIdx, endIdx + 1).reduce(
    (acc, r) => {
      acc.w += r.wins
      acc.l += r.losses
      acc.m += r.matches
      return acc
    },
    { w: 0, l: 0, m: 0 }
  )
  const decidedN = decided.w + decided.l
  const decidedWR = decidedN ? decided.w / decidedN : null

  const fromLabel = rows[startIdx]?.date
  const toLabel = rows[endIdx]?.date

  return (
    <div className={cn("rounded-lg border bg-card p-4 shadow-sm", className)}>
      <div className="mb-2 flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-0.5">
          <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Window
          </div>
          <div className="text-base font-semibold tracking-tight">
            {fromLabel ? fmtFull(fromLabel) : "—"}{" "}
            <span className="text-muted-foreground">→</span>{" "}
            {toLabel ? fmtFull(toLabel) : "—"}
          </div>
          <div className="text-xs text-muted-foreground">
            {decided.m} matches · {decided.w}–{decided.l} ({pct(decidedWR)} match WR)
            {total > decided.m && (
              <> · <span className="opacity-60">{total} total</span></>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {PRESETS.map((p) => {
            const active =
              value.kind === "preset" && value.days === p.days
            return (
              <Button
                key={p.label}
                variant={active ? "default" : "outline"}
                size="sm"
                onClick={() => onChange({ kind: "preset", days: p.days })}
                className="h-8 px-3"
              >
                {p.label}
              </Button>
            )
          })}
        </div>
      </div>

      <div className="h-[150px]">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={rows}
            margin={{ top: 6, right: 8, bottom: 0, left: -10 }}
          >
            <defs>
              <linearGradient id="match-grad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-primary)" stopOpacity={0.5} />
                <stop offset="100%" stopColor="var(--color-primary)" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" opacity={0.5} />
            <XAxis
              dataKey="date"
              tickFormatter={fmt}
              tick={{ fontSize: 10, fill: "var(--color-muted-foreground)" }}
              axisLine={false}
              tickLine={false}
              minTickGap={20}
            />
            <YAxis
              tick={{ fontSize: 10, fill: "var(--color-muted-foreground)" }}
              axisLine={false}
              tickLine={false}
              width={24}
              allowDecimals={false}
            />
            <Tooltip
              cursor={{ fill: "var(--color-muted)", opacity: 0.4 }}
              content={({ active, payload }: any) => {
                if (!active || !payload?.length) return null
                const r: TimelineRow = payload[0].payload
                return (
                  <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
                    <div className="font-medium text-foreground">{fmtFull(r.date)}</div>
                    <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5">
                      <span className="text-muted-foreground">Matches</span>
                      <span className="text-right font-mono">{r.matches}</span>
                      <span className="text-emerald-500">Wins</span>
                      <span className="text-right font-mono">{r.wins}</span>
                      <span className="text-rose-500">Losses</span>
                      <span className="text-right font-mono">{r.losses}</span>
                      <span className="text-muted-foreground">Win rate</span>
                      <span className="text-right font-mono">{pct(r.winrate)}</span>
                    </div>
                  </div>
                )
              }}
            />
            <Area
              type="monotone"
              dataKey="matches"
              stroke="var(--color-primary)"
              strokeWidth={1.5}
              fill="url(#match-grad)"
              isAnimationActive={false}
            />
            <Bar
              dataKey="wins"
              fill="var(--color-emerald-500, oklch(0.696 0.17 162.48))"
              opacity={0.85}
              isAnimationActive={false}
              barSize={6}
            />
            <Bar
              dataKey="losses"
              fill="var(--color-rose-500, oklch(0.645 0.246 16.439))"
              opacity={0.65}
              isAnimationActive={false}
              barSize={6}
            />
            <Brush
              dataKey="date"
              height={22}
              stroke="var(--color-primary)"
              fill="var(--color-muted)"
              startIndex={brush[0]}
              endIndex={brush[1]}
              onChange={onBrushChange}
              tickFormatter={fmt}
              travellerWidth={10}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

/** Convert a RangeMode into the params API endpoints accept. */
export function rangeToParams(value: RangeMode): RangeParams {
  if (value.kind === "preset") {
    return value.days === "all" ? {} : { days: value.days }
  }
  return { from: value.from, to: value.to + 86399 } // include all of the "to" day
}

/** Default range used by every page until the user picks otherwise. */
export const DEFAULT_RANGE: RangeMode = { kind: "preset", days: 30 }
