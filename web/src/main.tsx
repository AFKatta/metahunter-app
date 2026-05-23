import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter } from "react-router-dom"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import "./index.css"
import App from "./App.tsx"
import { ThemeProvider } from "@/components/ThemeProvider"
import { FormatProvider } from "@/components/FormatProvider"
import { AccountProvider } from "@/components/AccountProvider"

const qc = new QueryClient({
  defaultOptions: {
    queries: {
      // Aggressive polling so live matches show up within seconds.
      staleTime: 2_000,
      refetchInterval: 5_000,
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={qc}>
        <AccountProvider>
          <FormatProvider>
            <BrowserRouter>
              <App />
            </BrowserRouter>
          </FormatProvider>
        </AccountProvider>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>
)
