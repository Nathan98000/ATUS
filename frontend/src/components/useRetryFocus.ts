import { useCallback, useEffect, useRef } from 'react'

/** The page's main heading; every page's `<h1>` carries `tabIndex={-1}` for this. */
export const PAGE_HEADING = '#main h1'

/**
 * Keeps keyboard focus on the page when "Try again" replaces what held it.
 *
 * Retrying a query that has no data sends it back to "pending", so the error
 * panel (or full-page state) holding the focused button unmounts, and focus
 * fell to `<body>`: a keyboard or screen-reader user had to start again from
 * the top of the document. `retry(run)` moves focus to the page's heading
 * (or to `target`, a selector for a closer heading) before the retry starts,
 * and until the retry has settled puts it back on whatever heading the page
 * then shows whenever focus has fallen to `<body>` — the analysis title, a
 * full-page state's heading, or the builder's. Pages announce progress and
 * the result through their live regions as before.
 *
 * `busy` is whether any query the retry refetches is fetching: the retry is
 * in flight from the render that first shows it busy until one that does not.
 */
export function useRetryFocus(busy: boolean): (run: () => unknown, target?: string) => void {
  const retry = useRef<{ phase: 'requested' | 'running'; target: string } | null>(null)

  useEffect(() => {
    const current = retry.current
    if (current === null) return
    if (focusFellToBody()) focusFirst(current.target, PAGE_HEADING)
    if (busy) current.phase = 'running'
    else if (current.phase === 'running') retry.current = null
  })

  return useCallback((run: () => unknown, target: string = PAGE_HEADING) => {
    retry.current = { phase: 'requested', target }
    focusFirst(target, PAGE_HEADING)
    void run()
  }, [])
}

function focusFellToBody(): boolean {
  return document.activeElement === null || document.activeElement === document.body
}

function focusFirst(...selectors: string[]): void {
  for (const selector of selectors) {
    const element = document.querySelector<HTMLElement>(selector)
    if (element) {
      element.focus()
      return
    }
  }
}
