import { NavLink } from "react-router-dom"
import { Crosshair } from "lucide-react"
import { cn } from "@/lib/utils"
import { LiveIndicator } from "@/components/LiveIndicator"
import { ThemeToggle } from "@/components/ThemeToggle"
import { FormatPicker } from "@/components/FormatPicker"
import { AccountPicker } from "@/components/AccountPicker"

const links = [
  { to: "/", label: "Overview", end: true },
  { to: "/matches", label: "Matches" },
  { to: "/matchups", label: "Matchups" },
]

export function Header() {
  return (
    <header className="sticky top-0 z-30 border-b bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-6">
        <div className="flex items-center gap-2 font-semibold tracking-tight">
          <Crosshair className="h-5 w-5 text-primary" />
          <span>Metahunter</span>
        </div>
        <nav className="flex gap-1 text-sm">
          {links.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.end}
              className={({ isActive }) =>
                cn(
                  "rounded-md px-3 py-1.5 transition-colors hover:bg-accent hover:text-accent-foreground",
                  isActive && "bg-accent text-accent-foreground"
                )
              }
            >
              {l.label}
            </NavLink>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-3">
          <AccountPicker />
          <FormatPicker />
          <LiveIndicator />
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}
