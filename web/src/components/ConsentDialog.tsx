import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Crosshair } from "lucide-react"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"

/**
 * Blocking first-run modal. The dashboard's root <App> component
 * gates every route behind this — if /api/upload/state reports
 * has_consented=false, this is what gets rendered instead.
 *
 * Two checkboxes:
 *   * Share my match data anonymously (REQUIRED — submit is
 *     disabled while unchecked). Matches the gate the website's
 *     download page enforces: you can't use Metahunter without it.
 *   * Show me on the public leaderboard (optional, default ON).
 *     Toggling this off keeps the data sharing but hides the
 *     user's name on every public surface (matchup tables, recent
 *     feed, leaderboard, etc.).
 */
export function ConsentDialog() {
  const qc = useQueryClient()
  const [agreed, setAgreed] = useState(false)
  const [leaderboard, setLeaderboard] = useState(true)

  const consent = useMutation({
    mutationFn: (opts: { leaderboard_opt_in: boolean }) =>
      api.uploadConsent(opts.leaderboard_opt_in),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["upload-state"] }),
  })

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-6 backdrop-blur">
      <div className="w-full max-w-lg rounded-xl border bg-card text-card-foreground shadow-2xl">
        <div className="flex items-center gap-3 border-b px-6 py-4">
          <Crosshair className="h-6 w-6 text-primary" />
          <div>
            <h2 className="text-lg font-semibold leading-tight">
              Welcome to Metahunter
            </h2>
            <p className="text-xs text-muted-foreground">
              One-time consent on first run.
            </p>
          </div>
        </div>

        <div className="space-y-4 px-6 py-5 text-sm">
          <p>
            Metahunter classifies every MTGO match you play, locally on
            your machine, and pools the results with everyone else
            running the app to power a community-wide meta site.
          </p>

          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs">
            <strong className="font-semibold">What gets shared:</strong>{" "}
            match metadata (your archetype, opponent's archetype,
            colours, cards observed, who won), uploaded to{" "}
            <code className="rounded bg-muted px-1">metahunter-api.fly.dev</code>.
            Your MTGO username goes up plaintext (you're consenting
            on your own behalf); every opponent's name is HMAC-hashed
            with a 32-byte secret that stays on your machine forever.
            Nothing else leaves this PC.
          </div>

          <label className="flex cursor-pointer items-start gap-3 rounded-md border p-3 hover:bg-accent/30">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-primary"
              checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)}
            />
            <span>
              <strong>I consent to anonymous match data sharing.</strong>{" "}
              You can revoke at any time from Settings — that wipes
              your server-side data and stops uploads.
            </span>
          </label>

          <label className="flex cursor-pointer items-start gap-3 rounded-md border p-3 hover:bg-accent/30">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 accent-primary"
              checked={leaderboard}
              onChange={(e) => setLeaderboard(e.target.checked)}
            />
            <span>
              Show my MTGO username on the public leaderboard. If
              unchecked, your matches still contribute to aggregate
              meta share / matchups, but your name is replaced by{" "}
              <code className="rounded bg-muted px-1">Anonymous</code>{" "}
              on every public surface.
            </span>
          </label>

          {consent.error && (
            <p className="text-xs text-rose-500">
              Couldn't reach the server: {String(consent.error)}
            </p>
          )}
        </div>

        <div className="flex items-center justify-between border-t px-6 py-4">
          <p className="text-xs text-muted-foreground">
            Closing this window won't dismiss the consent gate.
          </p>
          <Button
            disabled={!agreed || consent.isPending}
            onClick={() => consent.mutate({ leaderboard_opt_in: leaderboard })}
          >
            {consent.isPending ? "Saving…" : "Continue to dashboard"}
          </Button>
        </div>
      </div>
    </div>
  )
}
