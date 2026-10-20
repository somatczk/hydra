# Hydra dashboard

Next.js 16.4 (App Router) frontend for the Hydra engine. Routes and their current state are listed in the root
`README.md` under "Dashboard routes".

## Scripts

Run from `dashboard/` with Node 26 (the CI and Docker image version):

```bash
npm ci             # install from package-lock.json
npm run dev        # dev server on http://localhost:3000
npm run lint       # eslint
npm run typecheck  # tsc --noEmit (TypeScript 7)
npm test           # vitest run (jsdom)
npm run build      # next build, standalone output
npm run start      # next start
```

`npm run lint` and `next build` use the TypeScript 6 API package (`typescript` is aliased to
`@typescript/typescript6`); `typecheck` uses TypeScript 7 (`@typescript/native`).

## Talking to the engine

- `src/lib/api.ts` calls `NEXT_PUBLIC_API_URL`, or the page's own origin when it is unset. Next inlines
  `NEXT_PUBLIC_*` at build time, so only the value present during `next build` counts.
- `next.config.ts` rewrites `/api/*` to `API_URL` (default `http://localhost:8000`). This is also fixed when
  `next build` runs.
- `docker/dashboard/Dockerfile` sets neither variable, so the image calls its own origin. In production, romulus
  Traefik routes `/api` and `/ws` on the same host to the engine, which is why that works. The runtime
  `NEXT_PUBLIC_API_URL` set in romulus and in `deploy/compose.yml` does not change the bundle.
- For local development, run the engine on `localhost:8000` and use `npm run dev`; the rewrite forwards `/api`.
  WebSockets go to the page origin (`ws://localhost:3000`), so set `NEXT_PUBLIC_WS_URL=http://localhost:8000` when
  you need live WebSocket updates. Do not point `NEXT_PUBLIC_API_URL` at another port: the engine's CSRF check
  refuses `Sec-Fetch-Site: same-site` mutations, so every POST from `:3000` to `:8000` gets 403 `csrf_rejected`.

## Errors and data state

API errors use the engine's envelope `{"detail": {"code", "message", "state"}}`. Read it with
`src/lib/apiState.ts` (`stateFromError`, `describeApiError`) and show it with `src/components/ui/DataStateBadge.tsx`
(`DataStateBadge`, `DataStateErrorCard`). Do not show defaults or empty lists in place of an error.

## Build output

`next.config.ts` sets `output: "standalone"`. The Docker image copies `.next/standalone`, `.next/static` and `public/`
and runs `node server.js` on port 3000.

## Version

The sidebar shows the Hydra release version (`APP_VERSION`), which `next.config.ts` reads from the root
`pyproject.toml` (`src/lib/releaseVersion.ts`); the image build copies that file to `/pyproject.toml`, next to `/app`.
`package.json`'s own `version` is not shown.

## Tests

Tests sit next to the code as `*.test.ts` / `*.test.tsx` and run with vitest + jsdom (`vitest.config.mts`). CI runs
lint, typecheck, test and build in the "Dashboard Build" job.
