import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"

/**
 * The gate: sign in, then say which MTGO account you play here.
 *
 * Sign-in deliberately happens in the system browser rather than in a
 * window we drew. The user types their password into Discord's own
 * page, on Discord's own domain, with the padlock they can check — and
 * this app never has the opportunity to see it. That is worth the
 * slightly clumsier hand-off.
 */

const BRAND: Record<string, { name: string; className: string }> = {
  discord: { name: "Discord", className: "bg-[#5865F2] text-white hover:bg-[#4752c4]" },
  google: { name: "Google", className: "bg-white text-neutral-900 hover:bg-neutral-200" },
}

export function SignIn() {
  const qc = useQueryClient()
  const [waiting, setWaiting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const status = useQuery({
    queryKey: ["auth-status"],
    queryFn: () => api.authStatus(),
    // While the browser tab is open we poll, so the app notices the
    // moment sign-in completes without the user coming back to click.
    refetchInterval: waiting ? 1500 : 15_000,
  })

  const providers = useQuery({
    queryKey: ["auth-providers"],
    queryFn: () => api.authProviders(),
    staleTime: 60_000,
  })

  const start = useMutation({
    mutationFn: (provider: string) => api.authStart(provider),
    onSuccess: () => setWaiting(true),
    onError: () => setError("Could not open your browser. Is this machine online?"),
  })

  useEffect(() => {
    if (status.data?.signed_in) setWaiting(false)
  }, [status.data?.signed_in])

  if (status.isLoading) return null

  if (status.data?.signed_in) {
    return <LinkAccount onDone={() => qc.invalidateQueries()} />
  }

  const list = providers.data?.providers ?? []
  const offline = providers.data?.offline

  return (
    <Shell
      title="Sign in to Metahunter"
      subtitle="Your account links this app to the website, so the matches it records become your own history there."
    >
      <div className="flex flex-col gap-3">
        {providers.isLoading && <div className="h-11 animate-pulse rounded-lg bg-muted" />}

        {list.map((p) => (
          <button
            key={p}
            onClick={() => start.mutate(p)}
            disabled={start.isPending}
            className={cn(
              "flex h-11 items-center justify-center rounded-lg text-sm font-semibold",
              "transition-colors disabled:opacity-60",
              BRAND[p]?.className ?? "bg-foreground text-background"
            )}
          >
            Continue with {BRAND[p]?.name ?? p}
          </button>
        ))}

        {offline && (
          <p className="rounded-lg border border-dashed p-4 text-center text-xs leading-relaxed text-muted-foreground">
            Can't reach the Metahunter server. Check your connection and try
            again — signing in needs to be online just this once.
          </p>
        )}

        {waiting && (
          <div className="rounded-lg border border-dashed p-4 text-center">
            <p className="text-sm font-medium">Waiting for your browser…</p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Finish signing in in the tab that just opened. This window will
              continue on its own.
            </p>
            {start.data?.url && (
              <p className="mt-2 break-all text-[10px] text-muted-foreground/70">
                Didn't open? Paste this into your browser:<br />{start.data.url}
              </p>
            )}
          </div>
        )}

        {error && <p className="text-center text-xs text-red-400">{error}</p>}
      </div>
    </Shell>
  )
}

/**
 * Second step: which MTGO account is this?
 *
 * The list comes from the MTGO folders on this machine, so it is what
 * has actually been played here rather than something typed in. One
 * account is claimed without asking; more than one is a real question
 * only the user can answer.
 */
function LinkAccount({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)

  const status = useQuery({ queryKey: ["auth-status"], queryFn: () => api.authStatus() })
  const candidates = useQuery({
    queryKey: ["claim-candidates"],
    queryFn: () => api.claimCandidates(),
  })

  const claim = useMutation({
    mutationFn: (name: string) => api.claimPlayer(name),
    onSuccess: async () => {
      setError(null)
      // The decks belong to the account, so push them as soon as there
      // is an account to push them to.
      void api.syncDecks().catch(() => undefined)
      await qc.invalidateQueries()
      onDone()
    },
    onError: (e: Error) => setError(e.message),
  })

  const rows = candidates.data?.candidates ?? []
  const claimed = status.data?.players ?? []

  // One obvious account and nothing claimed yet: claim it rather than
  // asking a question with a single answer.
  useEffect(() => {
    if (claimed.length > 0 || claim.isPending || claim.isSuccess) return
    const only = rows.filter((r) => r.claimable && !r.claimed_by_you)
    if (rows.length === 1 && only.length === 1) claim.mutate(only[0].mtgo_username)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows.length, claimed.length])

  if (claimed.length > 0) return null // gate satisfied; App renders on

  if (candidates.isLoading) return null

  if (rows.length === 0) {
    return (
      <Shell
        title="No MTGO account found"
        subtitle="Metahunter reads the match logs MTGO writes on this machine, and hasn't found any yet."
      >
        <p className="text-sm leading-relaxed text-muted-foreground">
          Play a match in MTGO with the client installed here, then reopen
          Metahunter. If you play on a different PC, install Metahunter there
          and sign in with the same account.
        </p>
      </Shell>
    )
  }

  return (
    <Shell
      title="Which account do you play?"
      subtitle={
        rows.length === 1
          ? "Confirm this is you, and your history will be linked to your account."
          : "More than one MTGO account has played on this machine. Pick the one that's yours."
      }
    >
      <ul className="flex flex-col gap-2">
        {rows.map((r) => (
          <li key={r.mtgo_username}>
            <button
              disabled={!r.claimable || claim.isPending}
              onClick={() => claim.mutate(r.mtgo_username)}
              className={cn(
                "flex w-full items-center justify-between gap-3 rounded-lg border p-3 text-left",
                "transition-colors disabled:cursor-not-allowed disabled:opacity-50",
                r.claimable && "hover:border-foreground/30 hover:bg-muted/40"
              )}
            >
              <span className="min-w-0">
                <span className="block truncate font-semibold">{r.mtgo_username}</span>
                <span className="text-xs text-muted-foreground">
                  {r.matches.toLocaleString()} matches recorded here
                </span>
              </span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {r.claimable ? "Link →" : "Already claimed"}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {rows.some((r) => !r.claimable) && (
        <p className="text-xs leading-relaxed text-muted-foreground">
          An account greyed out here is already linked to a different Metahunter
          account. If it's really yours, get in touch and we'll sort it out.
        </p>
      )}

      {error && <p className="text-xs text-red-400">{error}</p>}
    </Shell>
  )
}

function Shell({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  return (
    <div className="grid min-h-svh place-items-center bg-background px-6">
      <div className="flex w-full max-w-md flex-col gap-6 py-10">
        <div>
          <div className="mb-6 flex items-center gap-2 text-lg font-bold tracking-tight">
            <span className="grid size-7 place-items-center rounded-full border-2 border-primary text-primary">
              ◎
            </span>
            Metahunter
          </div>
          <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
            {subtitle}
          </p>
        </div>
        {children}
      </div>
    </div>
  )
}
