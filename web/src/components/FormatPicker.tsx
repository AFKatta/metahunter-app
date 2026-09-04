import { api } from "@/lib/api"
import { usePersistedQuery } from "@/lib/persist"
import { useFormat } from "@/components/FormatProvider"
import { useAccount } from "@/components/AccountProvider"
import { cn } from "@/lib/utils"

/** Top-bar dropdown that scopes the dashboard to one MTGO format.
 *
 * Reads /api/formats to discover what's actually in the user's data,
 * then offers a select of every format with ≥1 match. When the user
 * only ever plays one format (the common case) the picker hides
 * itself — no clutter for the 99% case. */

export function FormatPicker() {
  const { format, setFormat } = useFormat()
  const { account } = useAccount()
  const { data, isLoading } = usePersistedQuery({
    // Re-query when the user switches account — the friend's account
    // may have a different set of formats from yours.
    queryKey: ["formats", account],
    queryFn: () => api.formats({ user: account || undefined }),
    staleTime: 60_000,
    refetchInterval: 60_000,
  })

  if (isLoading || !data) return null

  // Sort by descending count so the most-played format comes first.
  const options = Object.entries(data)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([name, n]) => ({ name, n }))

  // One format only → nothing to choose between.
  if (options.length <= 1) return null

  // If the persisted format isn't present in the current data set
  // (e.g. user just deleted their .sqlite, or switched accounts),
  // fall back to the most-played format silently.
  const valueExists = options.some((o) => o.name === format)
  const currentValue = valueExists ? format : options[0].name

  return (
    <label
      className={cn(
        "flex items-center gap-1.5 text-xs text-muted-foreground",
        "select-none",
      )}
    >
      <span className="hidden sm:inline">Format</span>
      <select
        value={currentValue}
        onChange={(e) => setFormat(e.target.value)}
        className={cn(
          "h-7 rounded-md border bg-background px-2 py-0.5 text-xs font-medium",
          "text-foreground transition-colors hover:bg-accent focus:outline-none",
          "focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        {options.map((o) => (
          <option key={o.name} value={o.name}>
            {o.name} ({o.n})
          </option>
        ))}
      </select>
    </label>
  )
}
