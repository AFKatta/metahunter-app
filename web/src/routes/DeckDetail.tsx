import { useMemo, useState } from "react"
import { Link, useParams } from "react-router-dom"
import { api, type DeckCard, type Openings, type TrendPoint } from "@/lib/api"
import { usePersistedQuery } from "@/lib/persist"
import { useAccount } from "@/components/AccountProvider"
import { LeagueFinishes } from "@/components/LeagueFinishes"
import { VersionPicker } from "@/components/VersionPicker"
import { ManaCost, ManaCurve } from "@/components/ManaCost"
import { ago, pct, record, winrateColor } from "@/lib/format"
import { cn } from "@/lib/utils"

/**
 * One saved deck: the exact 75, and how it has actually performed.
 *
 * The list is ground truth, read from MTGO's own deck file. The record
 * beside it comes only from matches where MTGO recorded which deck was
 * registered — never from inference. A deck with no captured
 * registrations shows no record at all, which is the honest answer.
 */

/** Order a Magic player expects to read a decklist in. */
const SPELL_TYPES = [
  "Creature",
  "Planeswalker",
  "Instant",
  "Sorcery",
  "Artifact",
  "Enchantment",
  "Battle",
] as const

function bucket(card: DeckCard): string {
  // A card MTGO knows and Scryfall does not has no type line to sort
  // on, so it gets its own group instead of being lumped in with the
  // artifacts as "Other".
  if (!card.resolved) return "Not yet identified"
  const t = (card.type_line || "").toLowerCase()
  // Order matters: an "Artifact Creature" is a creature to anyone
  // reading a curve, and a land is a land whatever else it says.
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
  // null means "whatever the server considers current" — the newest
  // list. Picking a version pins the whole page to it: the cards, the
  // record, the matchups and the league runs are all that list's.
  const [version, setVersion] = useState<string | null>(null)

  const q = usePersistedQuery({
    queryKey: ["decklist", id, account, version],
    queryFn: () =>
      api.decklist(id, {
        user: account || undefined,
        version: version || undefined,
      }),
    enabled: Boolean(id),
    staleTime: 30_000,
  })

  const groups = useMemo(() => {
    const main = q.data?.maindeck ?? []
    const spells = SPELL_TYPES.map((t) => ({
      type: t,
      cards: main.filter((c) => bucket(c) === t),
    })).filter((g) => g.cards.length > 0)
    const lands = main.filter((c) => bucket(c) === "Land")
    for (const t of ["Other", "Not yet identified"]) {
      const rest = main.filter((c) => bucket(c) === t)
      if (rest.length) spells.push({ type: t as never, cards: rest })
    }
    // Spells the curve cannot place, so the two counts can be shown to
    // agree rather than quietly differing by three.
    const uncharted = main
      .filter((c) => !c.resolved)
      .reduce((n, c) => n + c.quantity, 0)
    return { spells, lands, uncharted }
  }, [q.data])

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
  const landCount = groups.lands.reduce((n, c) => n + c.quantity, 0)

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
              {d.format} · {d.maindeck_count} maindeck / {d.sideboard_count}{" "}
              sideboard · edited {ago(d.modified_at)}
              {d.versions.length > 1 && (
                <>
                  {" · "}
                  <span title="This deck has been edited; you are seeing one of its lists.">
                    {d.versions.findIndex((v) => v.signature === d.version) === 0
                      ? "latest list"
                      : "an earlier list"}{" "}
                    of {d.versions.length}
                  </span>
                </>
              )}
            </p>
          </div>
          <div className="flex gap-6">
            <Stat
              label={d.version_pinned ? "This list" : "Record"}
              value={d.winrate == null ? "—" : pct(d.winrate)}
              sub={
                d.wins + d.losses > 0
                  ? record(d.wins, d.losses) +
                    (d.version_pinned ? "" : d.versions.length > 1 ? " · all lists" : "")
                  : "no confirmed games"
              }
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

      {/* ---- decklist: spells left, lands + sideboard right, art rail ---- */}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_260px]">
        <section className="flex flex-col gap-4">
          {groups.spells.map((g) => (
            <CardGroup
              key={g.type}
              title={g.type}
              cards={g.cards}
              onHover={setPreview}
            />
          ))}
        </section>

        <section className="flex flex-col gap-4">
          {groups.lands.length > 0 && (
            <CardGroup title="Lands" cards={groups.lands} onHover={setPreview} />
          )}
          {d.sideboard.length > 0 && (
            <CardGroup
              title="Sideboard"
              cards={d.sideboard}
              onHover={setPreview}
            />
          )}
        </section>

        <aside className="flex flex-col gap-4">
          {/* Fixed-size preview: a card that resizes as you move between
              rows makes the whole column jitter. */}
          <div className="overflow-hidden rounded-xl border bg-muted"
               style={{ aspectRatio: "488 / 680" }}>
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

          <VersionPicker
            versions={d.versions}
            selected={d.version}
            onSelect={setVersion}
            pinned={d.version_pinned}
            onClear={() => setVersion(null)}
          />

          <ManaCurve curve={d.curve} uncharted={groups.uncharted} />

          <div className="rounded-lg border bg-card/60 p-3">
            <div className="flex justify-between font-mono text-[11px] tabular-nums text-muted-foreground">
              <span>{d.maindeck_count - landCount} spells</span>
              <span>{landCount} lands</span>
            </div>
          </div>
        </aside>
      </div>

      {/* ---- performance ---- */}
      <div className="grid gap-6 lg:grid-cols-2 xl:grid-cols-3">
        <Panel title="Matchups">
          {d.matchups.length === 0 ? (
            <Empty>
              No confirmed matches with this deck yet. Records appear once
              MTGO logs it being registered while Metahunter is open.
            </Empty>
          ) : (
            <ul className="flex flex-col">
              {d.matchups.slice(0, 15).map((m) => (
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
            <Empty>Nothing confirmed for this deck yet.</Empty>
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
                    {h.event_kind && h.event_kind !== "unknown" && (
                      <span className="shrink-0 rounded border px-1 text-[9px] uppercase text-muted-foreground">
                        {h.event_kind}
                      </span>
                    )}
                    <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
                      {h.score}
                    </span>
                    <span className="w-14 shrink-0 text-right text-[11px] text-muted-foreground">
                      {h.played_at ? ago(h.played_at) : ""}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="League finishes">
          <LeagueFinishes leagues={d.leagues} />
        </Panel>

        {d.openings?.games > 0 && (
          <Panel title="Opening hands">
            <OpeningHands openings={d.openings} />
          </Panel>
        )}

        {d.trend?.length > 1 && (
          <Panel title="Win rate over time">
            <WinrateOverTime points={d.trend} />
          </Panel>
        )}
      </div>
    </main>
  )
}

/* ------------------------------------------------------------------ */

/**
 * Mulligan depth, and how those games went.
 *
 * One row per GAME rather than per match — a mulligan is a per-game
 * decision, and a three-game match contributes three openings.
 *
 * Hand size only, and the note says so. MTGO's game log names a card
 * once it is played or revealed, and nothing is revealed before turn
 * one, so what was actually in an opening hand is recorded in no file
 * MTGO writes. There is no "lands kept" row because there is no
 * honest way to produce one.
 */
function OpeningHands({ openings }: { openings: Openings }) {
  return (
    <>
      <ul className="flex flex-col gap-1">
        {openings.by_kept.map((r) => (
          <li key={r.kept} className="flex items-center gap-2 text-sm">
            <span className="w-16 shrink-0">
              {r.kept}
              {r.mulligans > 0 && (
                <span className="ml-1 text-[11px] text-muted-foreground">
                  {r.mulligans === 1 ? "1 mull" : `${r.mulligans} mulls`}
                </span>
              )}
            </span>
            <span className="h-1.5 flex-1 overflow-hidden rounded-sm bg-muted">
              <span
                className="block h-full bg-primary/60"
                style={{ width: `${r.share * 100}%` }}
              />
            </span>
            <span className="w-12 shrink-0 text-right tabular-nums text-muted-foreground">
              {r.games}
            </span>
            <span
              className={cn(
                "w-14 shrink-0 text-right tabular-nums",
                r.winrate == null
                  ? "text-muted-foreground/50"
                  : winrateColor(r.winrate),
              )}
              title={
                r.winrate == null
                  ? "Too few games to state a win rate"
                  : `${r.wins} of ${r.games}`
              }
            >
              {r.winrate == null ? "—" : pct(r.winrate)}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
        {openings.games} games. Hand size only — MTGO's log does not
        record what was in a hand, so how many lands you kept is not
        something any file can tell us.
      </p>
    </>
  )
}

/**
 * Win rate week by week.
 *
 * Bar height is the win rate, bar opacity how many matches that week
 * rested on — so a 100% week off two matches reads as the thin
 * evidence it is rather than as a peak.
 */
function WinrateOverTime({ points }: { points: TrendPoint[] }) {
  const busiest = Math.max(...points.map((p) => p.matches), 1)
  return (
    <>
      <div className="flex h-24 items-end gap-[3px]">
        {points.map((p) => (
          <div
            key={p.week}
            className="group relative flex-1"
            title={`${p.week}: ${p.wins}-${p.matches - p.wins}`}
          >
            <div
              className={cn(
                "w-full rounded-sm",
                p.winrate != null && p.winrate >= 0.5
                  ? "bg-emerald-500"
                  : "bg-red-500",
              )}
              style={{
                height: `${Math.max((p.winrate ?? 0) * 96, 2)}px`,
                opacity: 0.35 + 0.65 * (p.matches / busiest),
              }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-muted-foreground">
        <span>{points[0]?.week}</span>
        <span>50% line is the midpoint</span>
        <span>{points[points.length - 1]?.week}</span>
      </div>
    </>
  )
}

function CardGroup({
  title,
  cards,
  onHover,
}: {
  title: string
  cards: DeckCard[]
  onHover: (c: DeckCard | null) => void
}) {
  const total = cards.reduce((n, c) => n + c.quantity, 0)
  return (
    <div>
      <h2 className="mb-1 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        {title} ({total})
      </h2>
      <ul className="flex flex-col">
        {cards.map((c) => (
          <li key={`${title}-${c.mtgo_id}`}>
            <div
              onMouseEnter={() => onHover(c)}
              className={cn(
                "flex items-center gap-2 rounded px-1 py-[3px] text-sm",
                "hover:bg-muted/50",
                !c.resolved && "opacity-60"
              )}
            >
              <span className="w-4 shrink-0 text-right font-mono text-xs tabular-nums text-muted-foreground">
                {c.quantity}
              </span>
              <span className="min-w-0 flex-1 truncate">{c.name}</span>
              <ManaCost cost={c.mana_cost} size={18} className="shrink-0" />
            </div>
          </li>
        ))}
      </ul>
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
  return (
    <p className="px-2 py-4 text-center text-xs leading-relaxed text-muted-foreground">
      {children}
    </p>
  )
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
          tone != null ? winrateColor(tone) : ""
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
