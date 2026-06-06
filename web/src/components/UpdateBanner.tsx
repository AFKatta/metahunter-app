import { useMutation, useQuery } from "@tanstack/react-query"
import { ArrowUpCircle, Download, X } from "lucide-react"
import { useState } from "react"
import { api } from "@/lib/api"

/**
 * Persistent banner shown when /api/updater/state reports an update
 * is available. Three states, in order:
 *
 *   1. Update available, still downloading      — progress bar
 *   2. Update available, installer downloaded   — "Install now" button
 *   3. No update                                — banner is hidden
 *
 * The user can dismiss the banner for the current session via the X.
 * The next page load (or 12h-tick from the worker) brings it back if
 * the update is still pending.
 */
export function UpdateBanner() {
  const [dismissed, setDismissed] = useState(false)
  const state = useQuery({
    queryKey: ["updater-state"],
    queryFn: () => api.updaterState(),
    refetchInterval: 30_000,  // pick up download progress without spamming
    refetchOnWindowFocus: false,
  })

  const install = useMutation({
    mutationFn: () => api.updaterInstall(),
  })

  const s = state.data
  if (!s || !s.update_available || dismissed) return null

  return (
    <div className="border-b border-primary/30 bg-primary/10 text-foreground">
      <div className="mx-auto flex max-w-7xl items-center gap-4 px-6 py-2.5 text-sm">
        <ArrowUpCircle className="h-4 w-4 shrink-0 text-primary" />
        <div className="min-w-0 flex-1">
          <span className="font-semibold">
            Metahunter {s.latest_version} is available
          </span>{" "}
          <span className="text-muted-foreground">
            (you're on {s.current_version}).
          </span>
          {s.download_pct !== null && (
            <span className="ml-2 text-muted-foreground">
              Downloading… {s.download_pct.toFixed(0)}%
            </span>
          )}
        </div>

        {s.downloaded ? (
          <button
            onClick={() => install.mutate()}
            disabled={install.isPending}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-bold text-primary-foreground hover:opacity-90 disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" />
            {install.isPending ? "Closing…" : "Install & restart"}
          </button>
        ) : s.download_pct !== null ? (
          <div className="h-1.5 w-32 overflow-hidden rounded-full bg-primary/20">
            <div
              className="h-full bg-primary transition-all"
              style={{ width: `${s.download_pct}%` }}
            />
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">queued</span>
        )}

        <button
          onClick={() => setDismissed(true)}
          aria-label="Dismiss until next launch"
          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  )
}
