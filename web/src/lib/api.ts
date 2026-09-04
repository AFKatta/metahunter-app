/** Thin typed client for the Metahunter API. */

export type Me = {
  user: string | null
  window_days: number | null
  total_matches: number
  decided_matches: number
  match_wins: number
  match_losses: number
  match_winrate: number | null
  game_wins: number
  game_losses: number
  game_winrate: number | null
  play_wins: number
  play_losses: number
  play_winrate: number | null
  draw_wins: number
  draw_losses: number
  draw_winrate: number | null
  first_match_at: number | null
  last_match_at: number | null
}

export type DeckRow = {
  archetype: string
  wins: number
  losses: number
  total: number
  winrate: number | null
}

export type MatchRow = {
  match_id: string
  log_mtime: number
  opponent: string
  your_deck: string
  their_deck: string
  match_winner: string | null
  result: "W" | "L" | "?"
  your_games: number
  their_games: number
  turns: number
  /** True if you were on the play in game 1, false if on the draw,
   *  null when the parser couldn't determine who chose to play first. */
  on_play: boolean | null
}

export type MatchesPage = {
  total: number
  page: number
  page_size: number
  items: MatchRow[]
}

export type MatchupRow = {
  your_deck: string
  their_deck: string
  wins: number
  losses: number
  total: number
  winrate: number | null
}

export type MatchDetail = {
  match_id: string
  log_path: string
  log_mtime: number
  players: string[]
  first_player: string | null
  turns: number
  match_winner: string | null
  score: [number, number] | null
  games: { winner: string | null; loser: string | null; by_concede: boolean }[]
  archetypes: Record<string, string>
  signatures: Record<string, { name: string; count: number }[]>
  cards_by_player: Record<string, string[]>
  user: string | null
}

export type MatchLog = {
  match_id: string
  log_path: string
  size_bytes: number
  truncated: boolean
  text: string
}

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} on ${url}`)
  return r.json() as Promise<T>
}

const qs = (params: Record<string, string | number | undefined | null>) => {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ""
}

export type TimelineRow = {
  date: string         // YYYY-MM-DD
  ts: number           // epoch seconds at UTC midnight
  matches: number
  wins: number
  losses: number
  winrate: number | null
}

export type RangeParams = {
  days?: number
  from?: number   // epoch seconds (inclusive lower bound)
  to?: number     // epoch seconds (inclusive upper bound)
  /** MTGO format scope ("Legacy" / "Vintage" / "Modern" / …). The
   *  backend defaults to Legacy when this is omitted. */
  format?: string
  /** MTGO account scope. Empty / omitted means "let the backend pick
   *  the primary account on this machine". */
  user?: string
}

export type FormatCounts = Record<string, number>

export type Account = {
  user: string
  matches: number
  last_played: number
}


/** A card as it appears in a saved MTGO deck. */
export type DeckCard = {
  mtgo_id: number
  quantity: number
  name: string
  mana_cost: string
  type_line: string
  cmc: number
  colors: string
  rarity: string
  set: string
  image: string | null
  art: string | null
  /** False when the catalog id is not in the card index (old promos). */
  resolved: boolean
}

export type KeyCard = {
  name: string
  quantity: number
  mana_cost: string
  type_line: string
  image: string | null
  art: string | null
}

/** One deck saved in the MTGO client, with how it has performed. */
export type DecklistRow = {
  id: string
  name: string
  format: string
  colors: string
  maindeck_count: number
  sideboard_count: number
  modified_at: number
  wins: number
  losses: number
  matches: number
  winrate: number | null
  last_played: number | null
  key_cards: KeyCard[]
  curve: Record<string, number>
  resolved_cards: number
}

export type DecklistsResponse = {
  decks: DecklistRow[]
  card_index_size: number
  /** The Scryfall card index is downloading; names are incomplete. */
  card_index_building: boolean
  /** Matches MTGO itself recorded a registered deck for. */
  attributed_matches: number
  total_matches: number
  /** Friendly games dropped before any of these numbers were computed. */
  excluded_friendly: number
}

export type DeckMatchup = {
  archetype: string
  wins: number
  losses: number
  matches: number
  winrate: number | null
}

export type DeckHistoryRow = {
  match_id: string
  played_at: number | null
  opponent: string
  opponent_archetype: string
  result: "W" | "L" | null
  score: string | null
  /** "league" | "tournament" | "casual" | "unknown", as MTGO recorded it. */
  event_kind: string
}

/** One rung of the 5-0 … 0-5 league ladder. */
export type LeagueRung = {
  record: string
  wins: number
  runs: number
}

export type LeagueSummary = {
  ladder: LeagueRung[]
  completed_runs: number
  trophies: number
  in_progress: {
    wins: number
    losses: number
    matches: number
    started_at: number | null
  } | null
  average_wins: number | null
  league_matches: number
  /** Entries that stopped short of five matches and are not scored. */
  abandoned_runs: number
}

export type DecklistDetail = {
  id: string
  name: string
  format: string
  colors: string
  modified_at: number
  maindeck: DeckCard[]
  sideboard: DeckCard[]
  maindeck_count: number
  sideboard_count: number
  wins: number
  losses: number
  winrate: number | null
  matchups: DeckMatchup[]
  history: DeckHistoryRow[]
  leagues: LeagueSummary
  distinct_opponents: number
  curve: Record<string, number>
  resolved_cards: number
}

export const api = {
  decklists: (r: { user?: string; format?: string; refresh?: boolean } = {}) =>
    get<DecklistsResponse>(
      `/api/decklists${qs({
        user: r.user,
        format: r.format,
        // qs() serialises scalars only; the backend parses "true"/"false".
        refresh: r.refresh ? "true" : undefined,
      })}`
    ),
  decklist: (id: string, r: { user?: string } = {}) =>
    get<DecklistDetail>(`/api/decklists/${encodeURIComponent(id)}${qs(r)}`),
  refreshCardIndex: () =>
    postJson<{ ok: boolean; cards: number }>("/api/decklists/refresh-cards", {}),
  health: () => get<{ ok: boolean; corpus_decks: number; archetypes: number }>("/api/health"),
  formats: (r: { user?: string } = {}) => get<FormatCounts>(`/api/formats${qs(r)}`),
  accounts: () => get<Account[]>("/api/accounts"),
  me: (r: RangeParams = {}) => get<Me>(`/api/me${qs(r)}`),
  decks: (r: RangeParams = {}) => get<DeckRow[]>(`/api/decks${qs(r)}`),
  opponents: (r: RangeParams = {}) => get<DeckRow[]>(`/api/opponents${qs(r)}`),
  timeline: (r: RangeParams = {}) => get<TimelineRow[]>(`/api/timeline${qs(r)}`),
  matches: (
    params: RangeParams & {
      your_deck?: string
      their_deck?: string
      opponent?: string
      result?: "W" | "L"
      page?: number
      page_size?: number
    }
  ) => get<MatchesPage>(`/api/matches${qs(params)}`),
  matchups: (r: RangeParams & { your_deck?: string } = {}) =>
    get<MatchupRow[]>(`/api/matchups${qs(r)}`),
  match: (id: string) => get<MatchDetail>(`/api/match/${id}`),
  matchLog: (id: string) => get<MatchLog>(`/api/match/${id}/log`),
  uploadState: () => get<UploadState>("/api/upload/state"),
  uploadConsent: (leaderboard_opt_in: boolean) =>
    postJson<UploadState>("/api/upload/consent", { leaderboard_opt_in }),
  uploadPatchLeaderboard: (leaderboard_opt_in: boolean) =>
    patchJson<UploadState>("/api/upload/leaderboard", { leaderboard_opt_in }),
  uploadWipe: () => del("/api/upload/all"),
  updaterState: () => get<UpdaterState>("/api/updater/state"),
  updaterCheck: () => postJson<UpdaterState>("/api/updater/check", {}),
  updaterInstall: () =>
    // The server kills itself mid-response when the installer fires;
    // we expect EITHER a 200 or a network error, both meaning "go".
    postJson<UpdaterState>("/api/updater/install", {}).catch(() => null),
}

export type UpdaterState = {
  current_version: string
  latest_version: string | null
  update_available: boolean
  installer_url: string | null
  release_notes: string | null
  error: string | null
  downloaded: boolean
  download_pct: number | null
  last_checked_at: number
  last_error: string | null
}

export type UploadState = {
  install_id: string
  consented_at: string | null
  has_consented: boolean
  leaderboard_opt_in: boolean
  server_url: string
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} on ${url}`)
  return r.json() as Promise<T>
}

async function patchJson<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} on ${url}`)
  return r.json() as Promise<T>
}

async function del(url: string): Promise<void> {
  const r = await fetch(url, { method: "DELETE" })
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} on ${url}`)
}
