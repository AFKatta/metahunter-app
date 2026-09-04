import { api } from "@/lib/api"
import { usePersistedQuery } from "@/lib/persist"
import { useAccount } from "@/components/AccountProvider"
import { cn } from "@/lib/utils"

/** Top-bar dropdown that switches the dashboard to a different MTGO
 *  account on the same machine. Driven by /api/accounts which returns
 *  only LOCAL accounts (players who dominate an AppFiles folder),
 *  filtering out frequent opponents who'd otherwise show up. */
export function AccountPicker() {
  const { account, setAccount } = useAccount()
  const { data, isLoading } = usePersistedQuery({
    queryKey: ["accounts"],
    queryFn: api.accounts,
    staleTime: 60_000,
    refetchInterval: 60_000,
  })

  if (isLoading || !data) return null
  if (data.length <= 1) return null

  // The accounts list is already sorted by descending match count, so
  // ``data[0]`` is the most-active account. We use that as the default
  // when the persisted choice is empty or no longer exists in the DB.
  const valueExists = data.some((a) => a.user === account)
  const currentValue = valueExists ? account : data[0].user

  return (
    <label
      className={cn(
        "flex items-center gap-1.5 text-xs text-muted-foreground",
        "select-none",
      )}
    >
      <span className="hidden sm:inline">Account</span>
      <select
        value={currentValue}
        onChange={(e) => setAccount(e.target.value)}
        className={cn(
          "h-7 rounded-md border bg-background px-2 py-0.5 text-xs font-medium",
          "text-foreground transition-colors hover:bg-accent focus:outline-none",
          "focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        {/* Account name only — no match counts, no "(default)" label.
         *  The most-active account is at the top of the list so it's
         *  what the dropdown picks when no preference is persisted. */}
        {data.map((a) => (
          <option key={a.user} value={a.user}>
            {a.user}
          </option>
        ))}
      </select>
    </label>
  )
}
