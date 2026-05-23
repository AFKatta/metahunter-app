import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"

/** The currently-selected MTGO format ("Legacy", "Vintage", etc.).
 *
 * Stored in localStorage so the user's choice persists across sessions.
 * Every API call in the app picks this up and passes it through as the
 * ``?format=`` query parameter, so the backend scopes results to one
 * format at a time. */

type Ctx = {
  format: string
  setFormat: (f: string) => void
}

const FormatContext = createContext<Ctx>({
  format: "Legacy",
  setFormat: () => {},
})

const STORAGE_KEY = "metahunter.format"

export function FormatProvider({ children }: { children: ReactNode }) {
  const [format, setFormat] = useState<string>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) || "Legacy"
    } catch {
      return "Legacy"
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, format)
    } catch {
      // localStorage may be disabled in private mode — ignore.
    }
  }, [format])

  const update = useCallback((f: string) => setFormat(f), [])
  const value = useMemo(() => ({ format, setFormat: update }), [format, update])

  return (
    <FormatContext.Provider value={value}>{children}</FormatContext.Provider>
  )
}

export function useFormat(): Ctx {
  return useContext(FormatContext)
}
