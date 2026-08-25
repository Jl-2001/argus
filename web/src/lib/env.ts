/**
 * The one place this app resolves where the Argus API lives. Every
 * other module reaches the API only through `src/api/client.ts`, which
 * reads `ARGUS_API_URL` from here -- no component or hook ever reads
 * `import.meta.env` itself or hardcodes a hostname.
 *
 * `VITE_ARGUS_API_URL` (set in `.env`/`.env.local`, or the shell that
 * runs `npm run dev`/`npm run build`) overrides the default. Vite only
 * exposes env vars prefixed `VITE_` to client code -- see
 * https://vite.dev/guide/env-and-mode.
 */

const DEFAULT_ARGUS_API_URL = "http://127.0.0.1:8088";

export const ARGUS_API_URL: string = (
  (import.meta.env.VITE_ARGUS_API_URL as string | undefined) ?? DEFAULT_ARGUS_API_URL
).replace(/\/+$/, ""); // no trailing slash -- src/api/client.ts always joins with a leading "/"
