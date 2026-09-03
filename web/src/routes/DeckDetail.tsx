import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link, useParams } from "react-router-dom"
import { api, type DeckCard } from "@/lib/api"
import { useAccount } from "@/components/AccountProvider"
import { ago, pct, record, winrateColor } from "@/lib/format"
import { cn } from "@/lib/utils"

/**
 * One saved deck: the exact 75, and how it has actually performed.
 *
 * The decklist is ground truth, read from MTGO's own file. The record
 * beside it is inference — see the note in Decks.tsx — so matches whose
 * attribution was ambiguous are marked rather than quietly counted.
 */

const TYPE_ORDER = [
  "Creature",
  "Planeswalker",
  "Instant",
  "Sorcery",
  "Artifact",
  "Enchantment",
  "Battle",
  "Land",
] as const

function bucket(card: DeckCard): string {
  const t = (card.type_line || "").toLowerCase()
  // Order matters: an "Artifact Creature" is a creature to a player
  // building a curve, and a "Land" is a land whatever else it says.
  if (t.includes("land")) return "Land"
  if (t.includes("creature")) return "Creature"
  if (t.includes("planeswalker")) return "Planeswalker"
  if (t.includes("instant")) return "Instant"
  if (t.includes("sorcery")) return "Sorcery"
  if (t.includes("battle")) return "Battle"
  if (t.includes("artifact")) return "Artifact"
  if (t.includes("enchantment")) return "Enchantment"
  return "Other"
}

export function DeckDetail() {
  const { id = "" } = useParams()
  const { account } = useAccount()
  const [preview, setPreview] = useState<DeckCard | null>(null)

  const q = useQuery({
    queryKey: ["decklist", id, account],
    queryFn: () => api.decklist(id, { user: account || undefined }),
    enabled: Boolean(id),
  })

  if (q.isLoading) {
    return (
      <main className="mx-auto w-full max-w-[1400px] px-6 py-8">
        <div className="h-8 w-64 animate-pulse rounded bg-muted" />
        <div className="mt-6 h-96 animate-pulse rounded-xl bg-muted/40" />
      </main>
    )
  }

  if (q.isError || !q.data) {
    return (
      <main className="mx-auto w-full max-w-3xl px-6 py-16 text-center">
        <p className="font-medium">Deck not found.</p>
        <Link to="/decks" className="mt-2 inline-block text-sm underline">
          Back to decks
        </Link>
      </main>
    )
  }

  const d = q.data
  const groups = TYPE_ORDER.map((t) => ({
    type: t,
    cards: d.maindeck.filter((c) => bucket(c) === t),
  })).filter((g) => g.cards.length > 0)

  return (
    <main className="mx-auto flex w-full max-w-[1400px] flex-col gap-6 px-6 py-8">
      <div>
        <Link
          to="/decks"
          className="text-xs text-muted-foreground underline-offset-4 hover:underline"
        >
          ← Decks
        </Link>
        <div className="mt-2 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-3xl font-bold tracking-tight">{d.name}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {d.format} · {d.maindeck_count} main / {d.sideboard_count} side ·
              edited {ago(d.modified_at)}
            </p>
          </div>
          <div className="flex gap-6">
            <Stat
              label="Record"
              value={d.winrate == null ? "—" : pct(d.winrate)}
              sub={record(d.wins, d.losses)}
              tone={d.winrate}
            />
            <Stat
              label="Opponents"
              value={String(d.distinct_opponents)}
              sub="distinct players"
            />
          </div>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        {/* ---- the list ---- */}
        <section className="flex flex-col gap-5">
          {groups.map((g) => (
            <div key={g.type}>
              <h2 className="mb-2 font-mono text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
                {g.type} ({g.cards.reduce((n, c) => n + c.quantity, 0)})
              </h2>
              <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 xl:grid-cols-4">
                {g.cards.map((c) => (
                  <CardRow key={c.mtgo_id} card={c} onHover={setPreview} />
                ))}
              </div>
            </div>
          ))}

          {d.sideboard.length > 0 && (
            <div>
              <h2 className="mb-2 font-mono text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
                Sideboard ({d.sideboard_count})
              </h2>
              <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 xl:grid-cols-4">
                {d.sideboard.map((c) => (
                  <CardRow key={`sb-${c.mtgo_id}`} card={c} onHover={setPreview} />
                ))}
              </div>
            </div>
          )}
        </section>

        {/* ---- performance ---- */}
        <aside className="flex flex-col gap-5">
          {/* Hovering a card shows it here rather than in a floating
              tooltip, which would fight the grid on small windows. */}
          <div className="hidden lg:block">
            <div className="aspect-[488/680] w-full overflow-hidden rounded-xl border bg-muted">
              {preview?.image ? (
                <img
                  src={preview.image}
                  alt={preview.name}
                  className="h-full w-full object-cover"
                />
              ) : (
                <div className="grid h-full place-items-center p-4 text-center text-xs text-muted-foreground">
                  Hover a card to preview it
                </div>
              )}
            </div>
          </div>

          <Panel title="Matchups">
            {d.matchups.length === 0 ? (
              <Empty>No recorded matches for this deck yet.</Empty>
            ) : (
              <ul className="flex flex-col">
                {d.matchups.slice(0, 12).map((m) => (
                  <li
                    key={m.archetype}
                    className="flex items-center justify-between gap-2 border-b py-1.5 text-sm last:border-0"
                  >
                    <span className="truncate" title={m.archetype}>
                      {m.archetype}
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
                        {record(m.wins, m.losses)}
                      </span>
                      <span
                        className={cn(
                          "w-10 text-right font-mono text-xs tabular-nums",
                          winrateColor(m.winrate)
                        )}
                      >
                        {m.winrate == null ? "—" : pct(m.winrate)}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title="Recent matches">
            {d.history.length === 0 ? (
              <Empty>Nothing played with this deck yet.</Empty>
            ) : (
              <ul className="flex flex-col">
                {d.history.slice(0, 15).map((h) => (
                  <li key={h.match_id} className="border-b last:border-0">
                    <Link
                      to={`/match/${h.match_id}`}
                      className="flex items-center gap-2 py-1.5 text-sm hover:bg-muted/40"
                    >
                      <span
                        className={cn(
                          "grid size-5 shrink-0 place-items-center rounded text-[10px] font-bold",
                          h.result === "W"
                            ? "bg-emerald-500/15 text-emerald-500"
                            : h.result === "L"
                            ? "bg-red-500/15 text-red-500"
                            : "bg-muted text-muted-foreground"
                        )}
                      >
                        {h.result ?? "?"}
                      </span>
                      <span className="min-w-0 flex-1 truncate">
                        {h.opponent_archetype}
                      </span>
                      <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
                        {h.score}
                      </span>
                      <span className="w-14 shrink-0 text-right text-[11px] text-muted-foreground">
                        {h.played_at ? ago(h.played_at) : ""}
                      </span>
                      {h.ambiguous && (
                        <span
                          title="This match also fits other near-identical saved decks."
                          className="shrink-0 text-[10px] text-amber-500/80"
                        >
                          ~
                        </span>
                      )}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </aside>
      </div>
    </main>
  )
}

function CardRow({
  card,
  onHover,
}: {
  card: DeckCard
  onHover: (c: DeckCard | null) => void
}) {
  return (
    <div
      onMouseEnter={() => onHover(card)}
      className={cn(
        "flex items-center gap-2 overflow-hidden rounded-md border bg-card px-1.5 py-1",
        !card.resolved && "opacity-60"
      )}
      title={`${card.quantity}x ${card.name}${card.mana_cost ? ` ${card.mana_cost}` : ""}`}
    >
      {card.art ? (
        <img
          src={card.art}
          alt=""
          loading="lazy"
          className="h-7 w-10 shrink-0 rounded-sm object-cover"
        />
      ) : (
        <div className="h-7 w-10 shrink-0 rounded-sm bg-muted" />
      )}
      <span className="w-4 shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
        {card.quantity}
      </span>
      <span className="min-w-0 flex-1 truncate text-xs">{card.name}</span>
    </div>
  )
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border bg-card p-3">
      <h2 className="mb-2 font-mono text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
        {title}
      </h2>
      {children}
    </section>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="py-3 text-center text-xs text-muted-foreground">{children}</p>
}

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub: string
  tone?: number | null
}) {
  return (
    <div className="text-right">
      <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "font-mono text-2xl font-semibold tabular-nums",
          tone !== undefined ? winrateColor(tone) : ""
        )}
      >
        {value}
      </div>
      <div className="font-mono text-[11px] tabular-nums text-muted-foreground">
        {sub}
      </div>
    </div>
  )
}
