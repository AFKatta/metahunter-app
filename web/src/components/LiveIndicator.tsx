import { useEffect, useState } from "react"
import { useIsFetching } from "@tanstack/react-query"
import { Wifi, WifiOff } from "lucide-react"
import { cn } from "@/lib/utils"

/** Tiny breathing dot + "Live" label.
 *
 *  - Green pulse when a query is in flight (we just refreshed).
 *  - Steady muted dot otherwise.
 *  - Red if /api/health fails for >10s (network/server down).
 */
export function LiveIndicator() {
  const fetching = useIsFetching()
  const [healthy, setHealthy] = useState(true)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  // Bump lastUpdated whenever a fetch completes.
  useEffect(() => {
    if (fetching === 0) setLastUpdated(Date.now())
  }, [fetching])

  // Light health probe every 8s, independent of React Query.
  useEffect(() => {
    let killed = false
    const tick = async () => {
      try {
        const r = await fetch("/api/health")
        if (!killed) setHealthy(r.ok)
      } catch {
        if (!killed) setHealthy(false)
      }
    }
    tick()
    const id = setInterval(tick, 8_000)
    return () => {
      killed = true
      clearInterval(id)
    }
  }, [])

  const [, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 1_000)
    return () => clearInterval(id)
  }, [])

  const secondsAgo =
    lastUpdated == null ? null : Math.max(0, Math.floor((Date.now() - lastUpdated) / 1000))

  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      {healthy ? (
        <>
          <span className="relative inline-flex h-2 w-2">
            <span
              className={cn(
                "absolute inline-flex h-full w-full rounded-full opacity-60",
                fetching > 0
                  ? "animate-ping bg-emerald-500"
                  : "bg-emerald-500/30"
              )}
            />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
          </span>
          <span>Live</span>
          {secondsAgo !== null && (
            <span className="tabular-nums">· {secondsAgo}s</span>
          )}
        </>
      ) : (
        <>
          <WifiOff className="h-3.5 w-3.5 text-rose-500" />
          <span className="text-rose-500">Backend offline</span>
        </>
      )}
      <Wifi className="hidden" />
    </div>
  )
}
