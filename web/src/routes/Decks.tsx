import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { api, type DecklistRow } from "@/lib/api"
import { useAccount } from "@/components/AccountProvider"
import { ago, pct, record, winrateColor } from "@/lib/format"
import { cn } from "@/lib/utils"

/**
 * Decks read straight out of the MTGO client.
 *
 * MTGO saves every deck you build to its own file and keeps it current
 * as you edit, so these are your real lists rather than something
 * reconstructed from cards seen in play.
 *
 * Match records are a different matter and the page is careful about
 * saying so. MTGO only records which deck you registered in its rolling
 * text log, which the app does not read yet, so a match is attributed by
 * comparing the cards you cast against each saved list. That cannot
 * separate near-identical variants of one shell, so a record built
 * mostly from ties is flagged rather than presented as fact.
 */

type SortKey = "recent" | "played" | "winrate" | "name"

const MANA: Record<string, string> = {
  W: "bg-amber-100 text-amber-900",
  U: "bg-sky-300 text-sky-950",
  B: "bg-neutral-700 text-neutral-100",
  R: "bg-red-400 text-red-950",
  G: "bg-emerald-400 text-emerald-950",
}

export function Decks() {
  const { account } = useAccount()
  const [sort, setSort] = useState<SortKey>("recent")
  const [query, setQuery] = useState("")
  const [format, setFormat] = useState<string>("all")
  // 65 saved decks with 55 played means ten dead tiles between the
  // ones you care about. Hidden by default, one click to see them.
  const [showUnplayed, setShowUnplayed] = useState(false)

  const decks = useQuery({
    queryKey: ["decklists", account],
    queryFn: () => api.decklists({ user: account || undefined }),
    staleTime: 30_000,
  })

  const formats = useMemo(() => {
    const s = new Set((decks.data?.decks ?? []).map((d) => d.format))
    return ["all", ...Array.from(s).sort()]
  }, [decks.data])

  const rows = useMemo(() => {
    let list = decks.data?.decks ?? []
    if (!showUnplayed) list = list.filter((d) => d.matches > 0)
    if (format !== "all") list = list.filter((d) => d.format === format)
    const q = query.trim().toLowerCase()
    if (q) list = list.filter((d) => d.name.toLowerCase().includes(q))

    const sorted = [...list]
    sorted.sort((a, b) => {
      switch (sort) {
        case "name":
          return a.name.localeCompare(b.name)
        case "played":
          return b.matches - a.matches
        case "winrate":
          // Decks with no games sort last: an absent rate is not a low
          // one, and floating them to the top buries the real answers.
          if (a.winrate == null && b.winrate == null) return b.matches - a.matches
          if (a.winrate == null) return 1
          if (b.winrate == null) return -1
          return b.winrate - a.winrate || b.matches - a.matches
        default:
          return (b.last_played ?? b.modified_at) - (a.last_played ?? a.modified_at)
      }
    })
    return sorted
  }, [decks.data, sort, query, format, showUnplayed])

  const played = (decks.data?.decks ?? []).filter((d) => d.matches > 0).length

  return (
    <main className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 px-6 py-8">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-bold tracking-tight">Decks</h1>
        <p className="text-sm text-muted-foreground">
          Read from your MTGO client. {decks.data
            ? `${decks.data.decks.length} saved, ${played} with recorded matches.`
            : "Loading…"}
        </p>
      </header>

      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search decks…"
          aria-label="Search decks"
          className="h-9 w-56 rounded-md border bg-background px-3 text-sm
                     placeholder:text-muted-foreground focus:outline-none
                     focus-visible:ring-2 focus-visible:ring-ring"
        />
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as SortKey)}
          aria-label="Sort decks"
          className="h-9 rounded-md border bg-background px-2 text-sm"
        >
          <option value="recent">Last played</option>
          <option value="played">Most played</option>
          <option value="winrate">Win rate</option>
          <option value="name">Name</option>
        </select>
        {formats.length > 2 && (
          <select
            value={format}
            onChange={(e) => setFormat(e.target.value)}
            aria-label="Filter by format"
            className="h-9 rounded-md border bg-background px-2 text-sm"
          >
            {formats.map((f) => (
              <option key={f} value={f}>
                {f === "all" ? "All formats" : f}
              </option>
            ))}
          </select>
        )}
        <label className="flex cursor-pointer select-none items-center gap-2 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={showUnplayed}
            onChange={(e) => setShowUnplayed(e.target.checked)}
            className="size-3.5 accent-current"
          />
          Show unplayed
        </label>

        <div className="ml-auto text-xs text-muted-foreground">
          {decks.data && (
            <>
              {decks.data.attributed_matches.toLocaleString()} of{" "}
              {decks.data.total_matches.toLocaleString()} matches matched to a deck
            </>
          )}
        </div>
      </div>

      {decks.isLoading && <SkeletonGrid />}

      {decks.isError && (
        <p className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm">
          Couldn't read your decks. Is MTGO installed on this machine?
        </p>
      )}

      {decks.data && rows.length === 0 && (
        <div className="rounded-lg border p-10 text-center">
          <p className="font-medium">No decks found.</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {query || format !== "all"
              ? "Nothing matches those filters."
              : "Save a deck in MTGO and it will appear here."}
          </p>
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {rows.map((d) => (
          <DeckCard key={d.id} deck={d} />
        ))}
      </div>
    </main>
  )
}

function DeckCard({ deck }: { deck: DecklistRow }) {
  const face = deck.key_cards.find((c) => c.art) ?? deck.key_cards[0]
  const mostlyGuessed =
    deck.matches > 0 && deck.ambiguous_matches / deck.matches > 0.5

  return (
    <Link
      to={`/decks/${encodeURIComponent(deck.id)}`}
      className="group flex flex-col overflow-hidden rounded-xl border bg-card
                 transition-colors hover:border-foreground/25
                 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {/* Card art as the deck's face — the fastest way to recognise a
          list at a glance, which is the whole job of this grid. */}
      <div className="relative h-28 overflow-hidden bg-muted">
        {face?.art ? (
          <img
            src={face.art}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover object-center opacity-80
                       transition-transform duration-300 group-hover:scale-105"
          />
        ) : (
          <div className="h-full w-full bg-gradient-to-br from-muted to-background" />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-card via-card/60 to-transparent" />
        <div className="absolute right-2 top-2 flex gap-1">
          {deck.colors.split("").map((c) => (
            <span
              key={c}
              title={c}
              className={cn(
                "grid size-5 place-items-center rounded-full text-[10px] font-bold",
                MANA[c] ?? "bg-neutral-500 text-white"
              )}
            >
              {c}
            </span>
          ))}
        </div>
      </div>

      <div className="flex flex-1 flex-col gap-2 p-3">
        <div className="min-w-0">
          <h2 className="truncate font-semibold leading-tight" title={deck.name}>
            {deck.name}
          </h2>
          <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
            <span className="rounded border px-1.5 py-0.5">{deck.format}</span>
            <span>
              {deck.last_played ? ago(deck.last_played) : "never played"}
            </span>
          </div>
        </div>

        {/* Key cards, which is how you actually tell two Grixis lists apart. */}
        {deck.key_cards.length > 0 && (
          <p className="truncate text-xs text-muted-foreground" title={
            deck.key_cards.map((c) => `${c.quantity}x ${c.name}`).join(", ")
          }>
            {deck.key_cards.slice(0, 3).map((c) => c.name).join(" · ")}
          </p>
        )}

        <div className="mt-auto flex items-end justify-between gap-2 pt-1">
          <div className="text-xs text-muted-foreground">
            {deck.maindeck_count}
            <span className="opacity-60">/{deck.sideboard_count}</span>
            {deck.matches > 0 && (
              <span className="ml-2">{deck.matches} matches</span>
            )}
          </div>
          <div className="text-right">
            {deck.winrate == null ? (
              <span className="text-xs text-muted-foreground">no games</span>
            ) : (
              <>
                <div
                  className={cn(
                    "font-mono text-sm font-semibold tabular-nums",
                    winrateColor(deck.winrate)
                  )}
                >
                  {pct(deck.winrate)}
                </div>
                <div className="font-mono text-[11px] tabular-nums text-muted-foreground">
                  {record(deck.wins, deck.losses)}
                </div>
              </>
            )}
          </div>
        </div>

        {mostlyGuessed && (
          <p
            className="text-[10px] leading-tight text-amber-500/80"
            title="These matches also fit other near-identical saved decks, so the record is an estimate."
          >
            record shared with similar lists
          </p>
        )}
      </div>
    </Link>
  )
}

function SkeletonGrid() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4" aria-hidden>
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="h-56 animate-pulse rounded-xl border bg-muted/40" />
      ))}
    </div>
  )
}
