import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ShieldAlert, Upload } from "lucide-react"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"

/**
 * In-app settings for the consent / upload subsystem.
 *
 *   * Read-only: install_id, server URL, when consent was granted.
 *   * Toggle: leaderboard visibility (PATCH /api/upload/leaderboard).
 *   * Danger: wipe server-side data (DELETE /api/upload/all). After
 *     wipe the consent gate re-shows on next load.
 */
export function Settings() {
  const qc = useQueryClient()
  const state = useQuery({
    queryKey: ["upload-state"],
    queryFn: () => api.uploadState(),
  })

  const flip = useMutation({
    mutationFn: (opt_in: boolean) => api.uploadPatchLeaderboard(opt_in),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["upload-state"] }),
  })

  const wipe = useMutation({
    mutationFn: () => api.uploadWipe(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["upload-state"] }),
  })

  const [confirmWipe, setConfirmWipe] = useState(false)

  const s = state.data

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Manage what gets shared with the central Metahunter server.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Upload className="h-4 w-4" />
            Upload status
          </CardTitle>
          <CardDescription>
            Anonymised match data is sent to{" "}
            {s ? <code className="rounded bg-muted px-1">{s.server_url}</code> : "…"}.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <Row label="Install ID">
            {s ? <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{s.install_id}</code> : "—"}
          </Row>
          <Row label="Consent granted">
            {s?.consented_at ? (
              <span>{new Date(s.consented_at).toLocaleString()}</span>
            ) : (
              <Badge variant="outline" className="border-rose-500/40 text-rose-500">Not yet</Badge>
            )}
          </Row>
          <Separator />
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="font-medium">Show my MTGO username on the public leaderboard</div>
              <p className="mt-1 text-xs text-muted-foreground">
                Either way, your matches feed into aggregate meta share
                + matchup winrates. Off just hides your name from
                public surfaces.
              </p>
            </div>
            <button
              role="switch"
              aria-checked={s?.leaderboard_opt_in ?? true}
              disabled={!s?.has_consented || flip.isPending}
              onClick={() => flip.mutate(!(s?.leaderboard_opt_in ?? true))}
              className={
                "relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
                (s?.leaderboard_opt_in ? "bg-primary" : "bg-muted")
              }
            >
              <span
                className={
                  "pointer-events-none inline-block h-5 w-5 transform rounded-full bg-background shadow ring-0 transition " +
                  (s?.leaderboard_opt_in ? "translate-x-5" : "translate-x-0")
                }
              />
            </button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-rose-600 dark:text-rose-400">
            <ShieldAlert className="h-4 w-4" />
            Wipe my server-side data
          </CardTitle>
          <CardDescription>
            Deletes the install record + every match you've uploaded
            from the central database. Your local match history stays
            on this PC. Re-consenting later re-uploads it as a fresh
            install.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!confirmWipe ? (
            <Button variant="destructive" onClick={() => setConfirmWipe(true)}>
              Wipe my server data
            </Button>
          ) : (
            <div className="flex items-center gap-3">
              <span className="text-sm">Are you sure? This cannot be undone.</span>
              <Button variant="ghost" onClick={() => setConfirmWipe(false)}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={wipe.isPending}
                onClick={() => wipe.mutate()}
              >
                {wipe.isPending ? "Wiping…" : "Yes, wipe everything"}
              </Button>
            </div>
          )}
          {wipe.isSuccess && (
            <p className="mt-3 text-sm text-emerald-600">
              Wiped. The consent dialog will re-appear next page load.
            </p>
          )}
          {wipe.error && (
            <p className="mt-3 text-sm text-rose-500">
              {String(wipe.error)}
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  )
}
