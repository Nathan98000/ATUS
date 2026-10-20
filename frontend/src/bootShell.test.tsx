/**
 * Audit F65: if the hashed entry bundle never loads, index.html still shows an
 * explanation — without any inline script or style, so the strict CSP stays.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act } from '@testing-library/react'
import { createRoot } from 'react-dom/client'
import { describe, expect, it } from 'vitest'

const FRONTEND = process.cwd()
const html = readFileSync(join(FRONTEND, 'index.html'), 'utf8')
const page = new DOMParser().parseFromString(html, 'text/html')

describe('index.html boot shell', () => {
  it('has no inline script or style (CSP: script-src/style-src self)', () => {
    const scripts = Array.from(page.querySelectorAll('script'))
    expect(scripts.length).toBeGreaterThan(0)
    for (const script of scripts) {
      expect(script.getAttribute('src')).toBeTruthy()
      expect(script.textContent?.trim()).toBe('')
    }
    expect(page.querySelectorAll('style')).toHaveLength(0)
    expect(page.querySelectorAll('[style]')).toHaveLength(0)
    expect(html).not.toMatch(/\son[a-z]+=/i) // no inline event handlers
  })

  it('ships a fallback inside #root, styled by an unhashed stylesheet', () => {
    const shell = page.querySelector('#root .boot-shell')
    expect(shell).not.toBeNull()
    expect(shell?.textContent).toContain('ATUS Explorer did not start')
    expect(shell?.querySelector('a')?.getAttribute('href')).toBe('')
    expect(page.querySelector('link[rel="stylesheet"][href="/boot.css"]')).not.toBeNull()

    const css = readFileSync(join(FRONTEND, 'public', 'boot.css'), 'utf8')
    // The explanation is hidden at first and revealed by a delayed animation.
    expect(css).toMatch(/\.boot-shell__stalled\s*{[^}]*visibility:\s*hidden/)
    expect(css).toMatch(/\.boot-shell__stalled\s*{[^}]*animation:[^;]*\d+s forwards/)
  })

  it('is replaced by the app as soon as React renders', async () => {
    const container = document.createElement('div')
    container.innerHTML = page.querySelector('#root')?.innerHTML ?? ''
    expect(container.querySelector('.boot-shell')).not.toBeNull()
    const root = createRoot(container)
    await act(async () => {
      root.render(<p>app</p>)
    })
    expect(container.querySelector('.boot-shell')).toBeNull()
    expect(container.textContent).toBe('app')
    root.unmount()
  })
})
