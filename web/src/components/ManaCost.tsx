/**
 * Mana costs drawn as real mana symbols.
 *
 * Costs arrive as Scryfall strings like "{1}{U}" or "{2}{W/U}{X}". Each
 * brace group is one symbol, so the string is split on the braces rather
 * than character by character — hybrid and Phyrexian symbols are several
 * characters inside one group and would otherwise be shredded.
 *
 * The glyphs are inline SVG, not an icon font or Scryfall's hosted SVGs.
 * This is a desktop app that reads local files: it has to look right
 * with no network, and a decklist is a hundred symbols on one screen,
 * so a hundred image requests would be the slowest thing on the page.
 * Paths are silhouettes rather than faithful reproductions — at 16px
 * the outline is all that survives anyway.
 */

/** Official Wizards mana colours, as used on the printed symbols. */
const FACE: Record<string, string> = {
  W: "#fffbd5",
  U: "#aae0fa",
  B: "#cbc2bf",
  R: "#f9aa8f",
  G: "#9bd3ae",
  C: "#cac5c0",
  S: "#e3e4e5",
}

/** Generic/colourless costs sit on the same grey as {C}. */
const GENERIC = "#cac5c0"

/** Symbols are dark-on-light whatever the page theme is, as in print. */
const INK = "#11100e"

/**
 * Glyph geometry, all in a 100×100 box centred on (50,50).
 *
 * Each is one path so it can be dropped into a symbol at any size. The
 * skull uses evenodd so its eye sockets cut through rather than being
 * painted over, which would break on the two-tone hybrid backgrounds.
 */
const GLYPH: Record<string, { d: string; evenOdd?: boolean }> = {
  // Eight-rayed sun. The rays are kept short against a broad centre so
  // it reads as a sun rather than an asterisk, and stays distinct from
  // the snowflake, which is the only other radial glyph here.
  W: {
    d:
      "M50 4 L61.5 22.3 L82.5 17.5 L77.7 38.5 L96 50 L77.7 61.5 " +
      "L82.5 82.5 L61.5 77.7 L50 96 L38.5 77.7 L17.5 82.5 L22.3 61.5 " +
      "L4 50 L22.3 38.5 L17.5 17.5 L38.5 22.3 Z",
  },
  // Water drop.
  U: { d: "M50 8 C42 28 16 50 16 66 a34 34 0 0 0 68 0 C84 50 58 28 50 8 Z" },
  // Skull: cranium, jaw, two sockets and a nose.
  B: {
    evenOdd: true,
    d:
      "M50 10 C29 10 14 25 14 46 c0 12 5 20 12 25 v10 c0 5 4 9 9 9 h30 " +
      "c5 0 9-4 9-9 V71 c7-5 12-13 12-25 C86 25 71 10 50 10 Z " +
      "M45 45 a9 9 0 1 0 -18 0 a9 9 0 1 0 18 0 Z " +
      "M73 45 a9 9 0 1 0 -18 0 a9 9 0 1 0 18 0 Z " +
      "M50 56 l-7 13 h14 Z",
  },
  // Flame: pointed and slightly leaning, round at the base.
  R: {
    d:
      "M56 4 c2 13-5 19-12 26 C34 40 26 50 26 61 a28 28 0 0 0 56 0 " +
      "c0-10-5-18-12-25 c-1 7-4 10-8 12 c5-14 2-31-6-44 Z",
  },
  // Tree: broad canopy over a short trunk.
  G: {
    d:
      "M50 6 a32 30 0 1 0 -7 59.4 V88 c0 4 3 7 7 7 s7-3 7-7 V65.4 " +
      "A32 30 0 0 0 50 6 Z",
  },
  // Colourless: the four-pointed Eldrazi star.
  C: { d: "M50 5 L64 36 L95 50 L64 64 L50 95 L36 64 L5 50 L36 36 Z" },
  // Snow: a six-spoke flake.
  S: {
    d:
      "M46 5 h8 v27 l13-13 6 6 -19 19 v11 l19-19 6 6 -13 13 h27 v8 h-27 " +
      "l13 13 -6 6 -19-19 v11 l19 19 -6 6 -13-13 v27 h-8 V78 L33 91 l-6-6 " +
      "19-19 V55 L27 74 l-6-6 13-13 H7 v-8 h27 L21 34 l6-6 19 19 V36 " +
      "L27 17 l6-6 13 13 Z",
  },
  // Phyrexian: the pierced circle, simplified to a ring and two hooks.
  P: {
    evenOdd: true,
    d:
      "M50 8 a42 42 0 1 0 0.1 0 Z M50 22 a28 28 0 1 1 -0.1 0 Z " +
      "M44 34 h12 v40 h-12 Z",
  },
}

/** Split "{1}{U}{U}" into ["1", "U", "U"]. */
export function parseManaCost(cost: string | null | undefined): string[] {
  if (!cost) return []
  return Array.from(cost.matchAll(/\{([^}]+)\}/g)).map((m) => m[1])
}

function Glyph({ code, scale = 1, dx = 0, dy = 0 }: {
  code: string
  scale?: number
  dx?: number
  dy?: number
}) {
  const g = GLYPH[code]
  if (!g) return null
  const t =
    scale === 1 && !dx && !dy
      ? undefined
      : `translate(${dx} ${dy}) translate(50 50) scale(${scale}) translate(-50 -50)`
  return (
    <path
      d={g.d}
      fill={INK}
      fillRule={g.evenOdd ? "evenodd" : undefined}
      transform={t}
    />
  )
}

function Label({ text, scale = 1, dx = 0, dy = 0 }: {
  text: string
  scale?: number
  dx?: number
  dy?: number
}) {
  return (
    <text
      x={50 + dx}
      y={51 + dy}
      textAnchor="middle"
      dominantBaseline="central"
      fontSize={64 * scale}
      fontWeight={700}
      fill={INK}
    >
      {text}
    </text>
  )
}

/** One symbol, drawn as a circle plus whatever belongs inside it. */
function Pip({ sym, px }: { sym: string; px: number }) {
  const code = sym.toUpperCase()
  const parts = code.split("/")
  const phyrexian = parts.includes("P")
  const colours = parts.filter((p) => p !== "P" && p in FACE)
  const nonColour = parts.filter((p) => p !== "P" && !(p in FACE))

  // Hybrid: two payment options, so the circle carries both. Split on
  // the diagonal the way the printed symbol does.
  const hybrid = !phyrexian && parts.length === 2 && colours.length >= 1

  let face = GENERIC
  if (colours.length === 1) face = FACE[colours[0]]

  const body = (() => {
    if (hybrid) {
      const [a, b] = parts
      return (
        <>
          {/* Bottom-right half painted over the base fill. */}
          <path d="M100 0 L100 100 L0 100 Z" fill={FACE[b] ?? GENERIC} />
          {a in GLYPH ? (
            <Glyph code={a} scale={0.42} dx={-19} dy={-19} />
          ) : (
            <Label text={a} scale={0.42} dx={-19} dy={-19} />
          )}
          {b in GLYPH ? (
            <Glyph code={b} scale={0.42} dx={19} dy={19} />
          ) : (
            <Label text={b} scale={0.42} dx={19} dy={19} />
          )}
        </>
      )
    }
    if (phyrexian && colours.length === 1) {
      return <Glyph code="P" scale={0.82} />
    }
    if (colours.length === 1) return <Glyph code={colours[0]} scale={0.74} />
    if (code in GLYPH) return <Glyph code={code} scale={0.74} />
    // Generic amounts, {X}, and anything unrecognised: show the text.
    return <Label text={nonColour[0] || code} />
  })()

  if (hybrid) face = FACE[parts[0]] ?? GENERIC
  else if (phyrexian && colours.length === 1) face = FACE[colours[0]]

  return (
    <svg
      viewBox="0 0 100 100"
      width={px}
      height={px}
      role="img"
      aria-label={`{${sym}}`}
      className="shrink-0"
      style={{ display: "block" }}
    >
      <title>{`{${sym}}`}</title>
      {/* Clip everything to the disc so a hybrid's diagonal cannot
          spill past the edge. */}
      <defs>
        <clipPath id={`mc-${sym.replace(/[^a-z0-9]/gi, "")}-${px}`}>
          <circle cx="50" cy="50" r="50" />
        </clipPath>
      </defs>
      <g clipPath={`url(#mc-${sym.replace(/[^a-z0-9]/gi, "")}-${px})`}>
        <circle cx="50" cy="50" r="50" fill={face} />
        {body}
      </g>
    </svg>
  )
}

export function ManaCost({
  cost,
  size = 18,
  className = "",
}: {
  cost: string | null | undefined
  /** Pixel diameter of one symbol. */
  size?: number
  className?: string
}) {
  const symbols = parseManaCost(cost)
  if (symbols.length === 0) return null

  return (
    <span
      className={`inline-flex items-center gap-[3px] ${className}`}
      style={{ lineHeight: 0 }}
    >
      {symbols.map((s, i) => (
        <Pip key={`${s}-${i}`} sym={s} px={size} />
      ))}
    </span>
  )
}

/**
 * Mana curve over the non-land maindeck.
 *
 * Bars are scaled against the tallest column rather than the deck size,
 * because the shape is what a player reads here — whether the deck is
 * front-loaded or top-heavy — not the absolute counts. Colour runs cool
 * to warm across the curve so the weight of a deck is visible before
 * any of the numbers are.
 */
const CURVE_COLOR = [
  "#5eead4", // 0
  "#38bdf8", // 1
  "#60a5fa", // 2
  "#a78bfa", // 3
  "#e879f9", // 4
  "#fb923c", // 5
  "#f87171", // 6
  "#ef4444", // 7+
]

export function ManaCurve({
  curve,
  uncharted = 0,
  className = "",
}: {
  curve: Record<string, number>
  /**
   * Spells whose mana value is unknown because Scryfall has not
   * published a mapping for their MTGO id yet. Called out rather than
   * quietly dropped, so the spell count here cannot silently disagree
   * with the one on the decklist.
   */
  uncharted?: number
  className?: string
}) {
  const buckets = ["0", "1", "2", "3", "4", "5", "6", "7+"]
  const values = buckets.map((b) => curve[b] ?? 0)
  const max = Math.max(1, ...values)
  const total = values.reduce((a, b) => a + b, 0)

  // Average mana value, ignoring lands. "7+" is counted as 7, which
  // understates a deck with an Emrakul in it, but the alternative is
  // inventing a number for a bucket that deliberately has no ceiling.
  const avg =
    total > 0
      ? values.reduce((sum, n, i) => sum + n * (i === 7 ? 7 : i), 0) / total
      : 0

  const PLOT = 96 // px of vertical room for the tallest bar

  return (
    <div className={className}>
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
          Mana curve
        </h3>
        <span
          className="font-mono text-[10px] tabular-nums text-muted-foreground"
          title={
            uncharted > 0
              ? `${uncharted} spell${uncharted === 1 ? "" : "s"} left out: ` +
                "Scryfall has no mana value for them yet."
              : undefined
          }
        >
          {uncharted > 0 ? `${total}/${total + uncharted}` : total} spells ·{" "}
          {avg.toFixed(2)} avg
        </span>
      </div>

      {/* The count labels sit above the bars, so the chart needs real
          clearance from the header or the tallest one collides with it. */}
      <div className="mt-4 flex items-end gap-1.5" style={{ height: PLOT + 18 }}>
        {buckets.map((b, i) => {
          const n = values[i]
          return (
            <div key={b} className="flex flex-1 flex-col items-center gap-1">
              <span
                className={
                  "font-mono text-[11px] font-medium tabular-nums " +
                  (n ? "text-foreground" : "text-muted-foreground/40")
                }
              >
                {n || ""}
              </span>
              <div
                className="w-full rounded-[3px]"
                style={{
                  height: `${(n / max) * PLOT}px`,
                  minHeight: n ? 3 : 0,
                  background: CURVE_COLOR[i],
                  opacity: n ? 0.9 : 0,
                }}
                title={`${n} spells at ${b}`}
              />
            </div>
          )
        })}
      </div>
      <div className="mt-1.5 flex gap-1.5 border-t pt-1.5">
        {buckets.map((b) => (
          <span
            key={b}
            className="flex-1 text-center font-mono text-[10px] text-muted-foreground"
          >
            {b}
          </span>
        ))}
      </div>
    </div>
  )
}
