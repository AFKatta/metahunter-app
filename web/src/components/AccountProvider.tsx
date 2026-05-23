import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"

/** Which MTGO account the dashboard is currently scoped to.
 *
 * MTGO's AppFiles folder can contain multiple accounts if more than
 * one person has logged into MTGO on this Windows session. Storing
 * the selection in localStorage means the user only has to pick once
 * per browser. The empty string means "let the backend pick the
 * primary account" — that's the default until the user makes an
 * explicit choice. */

type Ctx = {
  account: string
  setAccount: (a: string) => void
}

const AccountContext = createContext<Ctx>({
  account: "",
  setAccount: () => {},
})

const STORAGE_KEY = "metahunter.account"

export function AccountProvider({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<string>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) || ""
    } catch {
      return ""
    }
  })

  useEffect(() => {
    try {
      if (account) localStorage.setItem(STORAGE_KEY, account)
      else localStorage.removeItem(STORAGE_KEY)
    } catch {
      // localStorage may be disabled in private mode — ignore.
    }
  }, [account])

  const update = useCallback((a: string) => setAccount(a), [])
  const value = useMemo(() => ({ account, setAccount: update }), [account, update])

  return (
    <AccountContext.Provider value={value}>{children}</AccountContext.Provider>
  )
}

export function useAccount(): Ctx {
  return useContext(AccountContext)
}
