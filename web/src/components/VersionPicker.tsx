import { useState } from "react"
import type { DeckVersion } from "@/lib/api"
import { cn } from "@/lib/utils"

/**
 * The lists this deck was played with, and the one saved now.
 *
 * Only those. MTGO rewrites a deck file on every click in the editor, so
 * keeping every save meant seventeen "versions" of one deck, fifteen of
 * them states nobody registered. A list is kept when a match was played
 * with it, and the current list is always shown as it is. Each played
 * list keeps its own record — a 3-2 with Tuesday's list and a 2-3 with
 * Wednesday's are two results, and merging them would hide whether the
 * change helped.
 */

function when(ts: number): string {
  const d = new Date(ts * 1000)
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })

  // Calendar days, not elapsed hours: something played at 10pm last night
  // is fourteen hours old, and calling that "today" would be wrong.
  const midnight = new Date()
  midnight.setHours(0, 0, 0, 0)
  const dayDiff = Math.floor((midnight.getTime() - d.getTime()) / 86_400_000)

  if (d.getTime() >= midnight.getTime()) return `Today ${time}`
  if (dayDiff < 1) return `Yesterday ${time}`
  if (dayDiff < 6) {
    return `${d.toLocaleDateString([], { weekday: "short" })} ${time}`
  }
  return d.toLocaleDateString([], { day: "numeric", month: "short" })
}

export function VersionPicker({
  versions,
  selected,
  onSelect,
  pinned = false,
  onClear,
}: {
  versions: DeckVersion[]
  selected: string | null
  onSelect: (signature: string) => void
  /** True when the page's numbers are narrowed to `selected`. */
  pinned?: boolean
  onClear?: () => void
}) {
  const [expanded, setExpanded] = useState<string | null>(null)

  // One list needs no picker; a control that does nothing is noise.
  if (versions.length < 2) return null

  return (
    <section className="rounded-xl border bg-card p-3">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="font-mono text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
          Lists
        </h2>
        {pinned && onClear ? (
          <button
            onClick={onClear}
            className="font-mono text-[10px] text-primary hover:underline"
          >
            show all lists
          </button>
        ) : (
          <span className="font-mono text-[10px] text-muted-foreground">
            {versions.length} lists
          </span>
        )}
      </div>

      {pinned && (
        <p className="mb-2 text-[10px] leading-relaxed text-muted-foreground">
          Showing this list only. The record, matchups and league runs below
          are its own.
        </p>
      )}

      <ul className="flex flex-col gap-1">
        {versions.map((v) => {
          const active = v.signature === selected
          const open = expanded === v.signature
          return (
            <li key={v.signature}>
              <button
                onClick={() => onSelect(v.signature)}
                title={
                  v.first_played
                    ? `First played ${new Date(v.first_played * 1000).toLocaleString()}`
                    : "Saved in MTGO now, not played yet"
                }
                className={cn(
                  "flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left text-sm",
                  "transition-colors hover:bg-muted/50",
                  active ? "border-foreground/30 bg-muted/40" : "border-transparent"
                )}
              >
                <span className="min-w-0 flex-1 truncate">
                  {v.current ? "Current list" : when(v.changed_at)}
                </span>
                {v.matches > 0 ? (
                  <span className="shrink-0 font-mono text-[11px] tabular-nums">
                    {v.wins}–{v.losses}
                  </span>
                ) : (
                  <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                    not played yet
                  </span>
                )}
              </button>

              {v.changes && (
                <div className="pl-2">
                  <button
                    onClick={() => setExpanded(open ? null : v.signature)}
                    className="mt-0.5 text-[10px] text-muted-foreground hover:text-foreground"
                  >
                    {open ? "hide changes" : summarise(v)}
                  </button>
                  {open && <Changes changes={v.changes} />}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}

/** "4 in, 4 out" — enough to tell whether a change was big or small. */
function summarise(v: DeckVersion): string {
  const c = v.changes!
  const added =
    c.maindeck_added.reduce((n, x) => n + x.quantity, 0) +
    c.sideboard_added.reduce((n, x) => n + x.quantity, 0)
  const removed =
    c.maindeck_removed.reduce((n, x) => n + x.quantity, 0) +
    c.sideboard_removed.reduce((n, x) => n + x.quantity, 0)
  if (added === 0 && removed === 0) return "same cards"
  return `${added} in, ${removed} out`
}

function Changes({ changes }: { changes: NonNullable<DeckVersion["changes"]> }) {
  const zones: { label: string; add: typeof changes.maindeck_added; rem: typeof changes.maindeck_removed }[] = [
    { label: "Maindeck", add: changes.maindeck_added, rem: changes.maindeck_removed },
    { label: "Sideboard", add: changes.sideboard_added, rem: changes.sideboard_removed },
  ]
  return (
    <div className="mb-1 mt-1 flex flex-col gap-1.5 rounded-md border border-dashed p-2">
      {zones.map((z) =>
        z.add.length === 0 && z.rem.length === 0 ? null : (
          <div key={z.label}>
            <div className="font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
              {z.label}
            </div>
            {z.add.map((x) => (
              <div key={`+${x.name}`} className="text-[11px] text-emerald-400">
                + {x.quantity} {x.name}
              </div>
            ))}
            {z.rem.map((x) => (
              <div key={`-${x.name}`} className="text-[11px] text-red-400">
                − {x.quantity} {x.name}
              </div>
            ))}
          </div>
        )
      )}
    </div>
  )
}
