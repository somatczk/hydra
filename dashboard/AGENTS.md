# dashboard

The Next.js 16 (App Router) frontend. The repo-wide rules in the root `AGENTS.md` apply; scripts, engine wiring
and the route list are in `dashboard/README.md`.

## Commands
Run from `dashboard/` with Node 26 (CI and the image use the same major):
```bash
npm ci && npm run lint && npx tsc --noEmit && npm test && npm run build   # the gate, all green before a commit
npx vitest run src/app/data/page.test.tsx                               # one file while iterating
```
`npx tsc --noEmit` / `npm run typecheck` is TypeScript 7 (`@typescript/native`); eslint and `next build` use the
TypeScript 6 API package (`typescript` is aliased to `@typescript/typescript6`). Do not "fix" the alias.

## Invariants
- Call the engine through `src/lib/api.ts` (`fetchApi`, `fetchApiText`). It adds `x-hydra-request: 1` to every
  non-GET request; the engine refuses a browser mutation without it (403 `csrf_rejected`). A raw `fetch` (e.g. a
  `FormData` upload) must spread `apiHeaders(method)` (`src/lib/api.test.ts`).
- Errors arrive as the engine envelope `{"detail": {"code", "message", "state"}}`. Read them with
  `src/lib/apiState.ts` (`stateFromError`, `describeApiError`, `parseErrorDetail`) and render them with
  `DataStateBadge` / `DataStateErrorCard`. Never show defaults, zeros or an empty list in place of an error
  (`src/lib/apiState.test.ts`, `src/components/ui/DataStateBadge.test.tsx`).
- Polling pages use `src/hooks/usePoll.ts`, which carries the data state (`loading`, `refreshing`, `ok`, `stale`, or
  the server's error state).
- Build UI from the house components in `src/components/ui` (exported from `index.ts`: `Button`, `Input`,
  `Select`, `DataCard`/`StatCard`, `Table`, `StatusBadge`, `ToastProvider`/`useToast`, `ErrorCard`,
  `DataStateBadge`/`DataStateErrorCard`; also `Section`, `Readout`, `TypedConfirm` for typed confirmations) before
  adding a native widget or a new component.
- Production responses carry the CSP in `next.config.ts`: first-party scripts only, `connect-src 'self'`, and
  `frame-src` limited to TradingView (the `/trading` chart is a cross-origin iframe). Dev builds send no CSP.
  A new third-party origin is a reviewed CSP change (`src/lib/securityHeaders.test.ts`).
- The sidebar version is `APP_VERSION`, read from `../pyproject.toml` by `src/lib/releaseVersion.ts`;
  `package.json`'s version is not shown (`src/lib/releaseVersion.test.ts`).
- Every page listed in `src/test/a11y.test.tsx` must pass axe with its data unavailable and loaded, and render no
  LIVE, real-money or armed copy in the all-paper world. Add a new page to that list.
- Tests sit next to the code as `*.test.ts(x)`, vitest + jsdom (`vitest.config.mts`, alias `@` = `src`). Page tests
  mount through `src/test/pageHarness.tsx` and mock `fetchApi`; shared payloads live in `src/test/fixtures.ts`.

## Where things are
- `src/app/<route>/page.tsx` one directory per route; `src/components/<area>/` feature components; `src/lib/` the
  API client, error state, formatting and derivations; `src/components/layout/navItems.ts` the navigation.
