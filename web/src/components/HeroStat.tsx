import type { ReactNode } from "react"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

export type HeroTone = "emerald" | "rose" | "amber" | "sky" | "primary" | "neutral"

const TONE_BG: Record<HeroTone, string> = {
  emerald:
    "from-emerald-500/15 to-emerald-500/0 ring-emerald-500/20",
  rose:
    "from-rose-500/15 to-rose-500/0 ring-rose-500/20",
  amber:
    "from-amber-500/15 to-amber-500/0 ring-amber-500/25",
  sky:
    "from-sky-500/15 to-sky-500/0 ring-sky-500/20",
  primary:
    "from-primary/15 to-primary/0 ring-primary/25",
  neutral:
    "from-muted to-transparent ring-border",
}

const TONE_TEXT: Record<HeroTone, string> = {
  emerald: "text-emerald-600 dark:text-emerald-300",
  rose: "text-rose-600 dark:text-rose-300",
  amber: "text-amber-600 dark:text-amber-300",
  sky: "text-sky-600 dark:text-sky-300",
  primary: "text-foreground",
  neutral: "text-foreground",
}

const TONE_DOT: Record<HeroTone, string> = {
  emerald: "bg-emerald-500",
  rose: "bg-rose-500",
  amber: "bg-amber-500",
  sky: "bg-sky-500",
  primary: "bg-primary",
  neutral: "bg-muted-foreground",
}

export function HeroStat({
  label,
  value,
  hint,
  tone = "primary",
  className,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: HeroTone
  className?: string
}) {
  return (
    <Card
      className={cn(
        "relative overflow-hidden bg-gradient-to-br ring-1 ring-inset",
        TONE_BG[tone],
        className
      )}
    >
      <div className="flex h-full flex-col p-5">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
          <span className={cn("h-1.5 w-1.5 rounded-full", TONE_DOT[tone])} />
          {label}
        </div>
        <div className={cn(
          "mt-2 font-mono text-5xl font-bold leading-none tracking-tight tabular-nums",
          TONE_TEXT[tone]
        )}>
          {value}
        </div>
        {hint && (
          <div className="mt-3 text-xs text-muted-foreground">{hint}</div>
        )}
      </div>
    </Card>
  )
}

export function pickWinRateTone(wr: number | null | undefined): HeroTone {
  if (wr == null) return "neutral"
  if (wr >= 0.6) return "emerald"
  if (wr >= 0.5) return "sky"
  if (wr >= 0.4) return "amber"
  return "rose"
}
