import { Moon, Sun, Monitor } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useTheme } from "@/components/ThemeProvider"
import { cn } from "@/lib/utils"

export function ThemeToggle() {
  const { theme, resolved, setTheme } = useTheme()
  const next = theme === "light" ? "dark" : theme === "dark" ? "system" : "light"
  const label =
    theme === "system" ? `System (${resolved})` : theme[0].toUpperCase() + theme.slice(1)
  const Icon = theme === "light" ? Sun : theme === "dark" ? Moon : Monitor
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(next)}
      title={`Theme: ${label}. Click for ${next}.`}
      className="h-8 w-8"
    >
      <Icon className={cn("h-4 w-4")} />
      <span className="sr-only">Toggle theme (current: {label})</span>
    </Button>
  )
}
