// Values test/global-setup.ts provides to the tests through `inject()`.
export {}

declare module 'vitest' {
  export interface ProvidedContext {
    /** Base URL of the local HTTP origin, e.g. http://127.0.0.1:53378. */
    localOrigin: string
    /** A loopback URL whose port refuses connections. */
    closedOrigin: string
  }
}
