import { useLayoutEffect, useRef, useState } from 'react'

/**
 * The rendered width of an element, so a chart can draw at its real size.
 * An SVG scaled from a fixed viewBox scales its text with it: 13px tick
 * labels became about 22px on a wide screen and about 5px on a phone. The
 * first measurement happens before paint; `fallback` is used until then,
 * and wherever ResizeObserver does not exist (jsdom).
 */
export function useElementWidth<T extends HTMLElement>(fallback: number) {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(fallback)

  useLayoutEffect(() => {
    const element = ref.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const measure = (measured: number) => {
      const next = Math.floor(measured)
      if (next > 0) setWidth(next)
    }
    measure(element.getBoundingClientRect().width)
    const observer = new ResizeObserver((entries) => {
      measure(entries[0]?.contentRect.width ?? 0)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return [ref, width] as const
}
