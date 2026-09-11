import { Component, type ErrorInfo, type ReactNode } from "react"
import { clearCache } from "@/lib/persist"

/**
 * A page that throws while rendering says so, instead of showing nothing.
 *
 * Without this, one exception anywhere in a page unmounts the whole app
 * and leaves an empty black window. That is what every deck page did
 * after the 0.7.1 update: it painted 0.7.0's saved copy of the deck,
 * which lacked a field the new page needed, and nothing on screen said
 * so — there was nothing a player could do but report it.
 *
 * The button offers the repair for that whole class of failure: drop the
 * app's saved responses and load fresh. It touches only that cache, never
 * matches or decks.
 */
type Props = { children: ReactNode }
type State = { error: Error | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[metahunter] page failed to render:", error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="mx-auto flex max-w-xl flex-col gap-3 px-6 py-16">
        <h1 className="text-lg font-semibold">This page couldn’t be shown.</h1>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Something on it failed while loading. Clearing the app’s saved data
          and reloading usually fixes it. Your matches and decks are not
          affected.
        </p>
        <pre className="overflow-x-auto rounded-md border bg-muted/40 p-2 text-[11px] text-muted-foreground">
          {error.message}
        </pre>
        <div className="flex gap-2">
          <button
            onClick={() => {
              clearCache()
              window.location.reload()
            }}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
          >
            Clear saved data and reload
          </button>
          <button
            onClick={() => window.history.back()}
            className="rounded-md border px-3 py-1.5 text-sm"
          >
            Go back
          </button>
        </div>
      </div>
    )
  }
}
