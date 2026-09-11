/**
 * Query results that survive closing the app.
 *
 * Reading 65 decks out of MTGO's files and joining them against the
 * match store takes a moment, and doing it on every launch meant the
 * Decks page opened as a wall of skeletons even though the answer was
 * almost always identical to last time. So the last response is kept in
 * localStorage and handed to React Query as `initialData` with its
 * original timestamp: the page paints from it immediately, React Query
 * sees data older than `staleTime` and refetches in the background, and
 * the fresh result replaces it without a flicker.
 *
 * Storage is best-effort throughout. A cache that fails to read, fails
 * to write, or holds something from an older build is simply ignored —
 * the app then behaves exactly as it did before this file existed.
 */
import { useEffect, useRef } from "react"
import { useQuery, type UseQueryOptions } from "@tanstack/react-query"

const PREFIX = "mh.cache."

/**
 * Bumped when a cached payload's shape or meaning changes; old entries
 * are dropped rather than shown. 3: deck colours are taken from mana
 * costs instead of Scryfall colour identity. 4: deck pages gained league
 * entries and played lists in 0.7.1 — and not bumping this for that
 * release is what turned every deck page black: the page painted 0.7.0's
 * saved copy first, which had no entries to list.
 */
const VERSION = 4

/**
 * The build that wrote an entry. A response saved by one release is never
 * shown by another, whether or not anybody remembered to bump VERSION.
 * Forgetting to is exactly how the black deck page shipped, so it should
 * not be something a release depends on remembering.
 */
const BUILD = __APP_BUILD__

/**
 * Entries larger than this are not written. Deck detail payloads carry
 * a full 75 with image URLs; a runaway response should not be allowed
 * to fill the origin's whole storage quota and evict everything else.
 */
const MAX_BYTES = 512 * 1024

/** Anything older than this is treated as absent rather than shown. */
const MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000

type Envelope<T> = { v: number; b: string; at: number; data: T }

function storageKey(key: unknown[]): string {
  return PREFIX + JSON.stringify(key)
}

function read<T>(key: unknown[]): Envelope<T> | null {
  let raw: string | null
  try {
    raw = window.localStorage.getItem(storageKey(key))
  } catch {
    return null // private mode, or storage disabled
  }
  if (!raw) return null
  try {
    const env = JSON.parse(raw) as Envelope<T>
    if (env.v !== VERSION || env.b !== BUILD) {
      // From another build: never shown, so not worth the storage either.
      try {
        window.localStorage.removeItem(storageKey(key))
      } catch {
        /* nothing to do */
      }
      return null
    }
    if (!env.at || Date.now() - env.at > MAX_AGE_MS) return null
    return env
  } catch {
    return null
  }
}

function write<T>(key: unknown[], data: T, at: number): void {
  let raw: string
  try {
    raw = JSON.stringify({ v: VERSION, b: BUILD, at, data } satisfies Envelope<T>)
  } catch {
    return // non-serialisable payload; nothing worth caching
  }
  if (raw.length > MAX_BYTES) return
  try {
    window.localStorage.setItem(storageKey(key), raw)
  } catch {
    // Over quota. Clear our own entries and try once more — everything
    // here is disposable, so dropping the lot is a fair trade for
    // keeping the newest response.
    try {
      clearCache()
      window.localStorage.setItem(storageKey(key), raw)
    } catch {
      /* give up quietly */
    }
  }
}

/** Drop every cached response. Exposed for a "reload from disk" action. */
export function clearCache(): void {
  try {
    const doomed: string[] = []
    for (let i = 0; i < window.localStorage.length; i++) {
      const k = window.localStorage.key(i)
      if (k && k.startsWith(PREFIX)) doomed.push(k)
    }
    doomed.forEach((k) => window.localStorage.removeItem(k))
  } catch {
    /* nothing to do */
  }
}

type Options<T> = Omit<
  UseQueryOptions<T, Error, T, readonly unknown[]>,
  "initialData" | "initialDataUpdatedAt"
> & { queryKey: readonly unknown[]; queryFn: () => Promise<T> }

/**
 * `useQuery`, with the last response restored from disk on first paint.
 *
 * `fromCache` tells the caller it is looking at a stored answer that is
 * currently being re-checked, which is worth a quiet marker in the UI
 * but should never be dressed up as a loading state.
 */
export function usePersistedQuery<T>(opts: Options<T>) {
  const key = opts.queryKey as unknown[]
  // Read once per mount. Re-reading on each render would resurrect a
  // stale payload after the fresh one has already replaced it.
  const seed = useRef<Envelope<T> | null | undefined>(undefined)
  if (seed.current === undefined) seed.current = read<T>(key)

  const q = useQuery<T, Error, T, readonly unknown[]>({
    ...opts,
    initialData: seed.current?.data,
    // The original timestamp, not now: this is what makes React Query
    // treat the restored value as stale and refresh it silently.
    initialDataUpdatedAt: seed.current?.at,
  })

  useEffect(() => {
    if (q.isSuccess && q.data !== undefined && !q.isFetching) {
      write(key, q.data, q.dataUpdatedAt || Date.now())
    }
    // dataUpdatedAt changes on every successful fetch, which is exactly
    // when the stored copy should be replaced.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.isSuccess, q.isFetching, q.dataUpdatedAt])

  return {
    ...q,
    /** True while showing a stored answer that is being re-checked. */
    fromCache: Boolean(seed.current) && q.isFetching && q.isSuccess,
  }
}
