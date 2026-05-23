import { Route, Routes } from "react-router-dom"
import { Header } from "@/components/Header"
import { Overview } from "@/routes/Overview"
import { Matches } from "@/routes/Matches"
import { Matchups } from "@/routes/Matchups"
import { MatchDetail } from "@/routes/MatchDetail"

function App() {
  return (
    <div className="min-h-svh bg-background text-foreground">
      <Header />
      <main>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/matches" element={<Matches />} />
          <Route path="/matchups" element={<Matchups />} />
          <Route path="/match/:id" element={<MatchDetail />} />
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
    </div>
  )
}

export default App
