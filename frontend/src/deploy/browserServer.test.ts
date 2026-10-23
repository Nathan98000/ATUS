/**
 * The E2E switch to the container's browsers (e2e/browserServer.ts,
 * scripts/e2e_in_container.sh): when it connects, how the browsers reach the
 * servers on this machine, and which platform the Tab-order checks assume.
 */
import { describe, expect, it } from 'vitest'

import {
  browserPlatform,
  browserServerConnectOptions,
  tabReachesAllControls,
} from '../../e2e/browserServer'

describe('browserServerConnectOptions', () => {
  it('launches local browsers, as always, when E2E_BROWSER_SERVER is unset or empty', () => {
    expect(browserServerConnectOptions(undefined)).toBeUndefined()
    expect(browserServerConnectOptions('')).toBeUndefined()
  })

  it('connects to the endpoint and tunnels loopback back to this machine (same origin)', () => {
    expect(browserServerConnectOptions('ws://127.0.0.1:8139/')).toEqual({
      wsEndpoint: 'ws://127.0.0.1:8139/',
      exposeNetwork: '<loopback>',
    })
    expect(browserServerConnectOptions('wss://browsers.example.test/pw')).toEqual({
      wsEndpoint: 'wss://browsers.example.test/pw',
      exposeNetwork: '<loopback>',
    })
  })

  it('refuses a value that is not a WebSocket URL instead of launching local browsers', () => {
    expect(() => browserServerConnectOptions('127.0.0.1:8139')).toThrow(/not a/)
    expect(() => browserServerConnectOptions('http://127.0.0.1:8139/')).toThrow(
      /not a WebSocket URL/,
    )
  })
})

describe('browserPlatform / tabReachesAllControls', () => {
  it('is the runner platform for local browsers and Linux for the container', () => {
    expect(browserPlatform(undefined, 'darwin')).toBe('darwin')
    expect(browserPlatform('', 'darwin')).toBe('darwin')
    expect(browserPlatform('ws://127.0.0.1:8139/', 'darwin')).toBe('linux')
    expect(browserPlatform(undefined, 'linux')).toBe('linux')
  })

  it('skips Tab-order checks only for WebKit on macOS', () => {
    expect(tabReachesAllControls('webkit', 'darwin')).toBe(false)
    expect(tabReachesAllControls('webkit', 'linux')).toBe(true)
    expect(tabReachesAllControls('firefox', 'darwin')).toBe(true)
    expect(tabReachesAllControls('chromium', 'darwin')).toBe(true)
    // WebKit in the container is checked, though the test runner is on a Mac.
    expect(
      tabReachesAllControls('webkit', browserPlatform('ws://127.0.0.1:8139/', 'darwin')),
    ).toBe(true)
  })
})
