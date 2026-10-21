/**
 * Browsers in the official Playwright Linux container
 * (scripts/e2e_in_container.sh). The container runs only `playwright
 * run-server`; `E2E_BROWSER_SERVER` names its endpoint and every project
 * connects to it instead of launching a browser here. The test runner,
 * `vite preview` and scripts/e2e_backend.py stay on this machine, unchanged.
 *
 * Pure (no Node or Playwright imports), so the unit tests can load it.
 */

/** The value of `use.connectOptions` for one browser-server endpoint. */
export interface BrowserServerConnectOptions {
  wsEndpoint: string
  exposeNetwork: string
}

/**
 * `use.connectOptions` for `E2E_BROWSER_SERVER`, or undefined when it is
 * unset or empty (browsers launch locally, as always).
 *
 * `exposeNetwork: '<loopback>'` tunnels the browsers' requests for
 * localhost, *.localhost, 127.0.0.1 and [::1] back through the test runner,
 * so the page loads the servers on this machine under the very origin the
 * tests use everywhere else (same-origin /api, the deployed headers); every
 * other host is resolved inside the container.
 */
export function browserServerConnectOptions(
  endpoint: string | undefined,
): BrowserServerConnectOptions | undefined {
  if (!endpoint) return undefined
  let url: URL
  try {
    url = new URL(endpoint)
  } catch {
    throw new Error(`E2E_BROWSER_SERVER=${endpoint} is not a URL (expected ws://host:port/)`)
  }
  if (url.protocol !== 'ws:' && url.protocol !== 'wss:') {
    throw new Error(
      `E2E_BROWSER_SERVER=${endpoint} is not a WebSocket URL (expected ws://host:port/, ` +
        'the endpoint `playwright run-server` prints)',
    )
  }
  return { wsEndpoint: endpoint, exposeNetwork: '<loopback>' }
}

/**
 * The platform the browsers run on: Linux in the container (the official
 * image is Ubuntu), otherwise the test runner's own (`process.platform`).
 */
export function browserPlatform(endpoint: string | undefined, runnerPlatform: string): string {
  return endpoint ? 'linux' : runnerPlatform
}

/**
 * Whether Tab reaches every link and button. WebKit on macOS follows
 * Safari's default keyboard setting, where Tab moves only between text
 * fields and menus, so Tab-order checks are skipped there; focused controls
 * are still operated with the keyboard. WebKit on Linux tabs to everything.
 */
export function tabReachesAllControls(browserName: string, platform: string): boolean {
  return !(browserName === 'webkit' && platform === 'darwin')
}
