import { useQuery } from "@tanstack/react-query"
import { Route, Routes } from "react-router-dom"
import { Header } from "@/components/Header"
import { ConsentDialog } from "@/components/ConsentDialog"
import { UpdateBanner } from "@/components/UpdateBanner"
import { Overview } from "@/routes/Overview"
import { Decks } from "@/routes/Decks"
import { DeckDetail } from "@/routes/DeckDetail"
import { Matches } from "@/routes/Matches"
import { Matchups } from "@/routes/Matchups"
import { MatchDetail } from "@/routes/MatchDetail"
import { Settings } from "@/routes/Settings"
import { api } from "@/lib/api"

function App() {
  // Block every route until consent is recorded. Cached forever
  // within the session — the gate flips closed only when the consent
  // mutation invalidates this key (or the user wipes from Settings).
  const consent = useQuery({
    queryKey: ["upload-state"],
    queryFn: () => api.uploadState(),
    staleTime: Infinity,
  })

  // While the state is loading, hold off rendering anything — a brief
  // blank screen is better than flashing the dashboard to a not-yet-
  // consented user.
  if (consent.isLoading) return null

  const needsConsent = consent.data ? !consent.data.has_consented : false

  return (
    <div className="min-h-svh bg-background text-foreground">
      <UpdateBanner />
      <Header />
      <main>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/decks" element={<Decks />} />
          <Route path="/decks/:id" element={<DeckDetail />} />
          <Route path="/matches" element={<Matches />} />
          <Route path="/matchups" element={<Matchups />} />
          <Route path="/match/:id" element={<MatchDetail />} />
          <Route path="/settings" element={<Settings />} />
          <Route
            path="*"
            element={
              <div className="mx-auto max-w-7xl px-6 py-12 text-sm text-muted-foreground">
                Page not found.
              </div>
            }
          />
        </Routes>
      </main>
      {needsConsent && <ConsentDialog />}
    </div>
  )
}

export default App
