# Frontend — Hobby Server Monitor

Astro + React UI for the LXD container monitor. Talks to the Falcon backend in
[../backend/](../backend/) over HTTP with session cookies.

> **Status: partially built.** The pages listed under
> [What exists today](#what-exists-today) work; several phases of UI are still
> unbuilt (see [What is not built yet](#what-is-not-built-yet)). `astro build`
> succeeds — "incomplete" here means missing features, not a broken build.
> This README is deliberately a starting point rather than full documentation;
> it will grow as the remaining phases land.

---

## Requirements

- **Node ≥ 22.12.0** (enforced by `engines` in `package.json`)
- The **backend running on port 8000** — every page except `/login` fetches
  from it, and there is no mock or fixture layer

## Setup

All commands run from this `frontend/` directory.

```bash
npm install
cp .env.example .env      # optional in dev; the fallback below covers it
npm run dev               # http://localhost:4321
```

## Commands

| Command           | Action                                                     |
| ----------------- | ---------------------------------------------------------- |
| `npm run dev`     | Dev server with HMR at `localhost:4321`                    |
| `npm run build`   | Static build to `./dist/`                                  |
| `npm run preview` | Serve the built output locally                             |
| `npx astro check` | TypeScript + Astro type check — run this before committing |

## Configuration

One variable, read in `src/lib/api.ts` and `src/pages/login.astro`:

| Variable              | Default                 | Purpose                        |
| --------------------- | ----------------------- | ------------------------------ |
| `PUBLIC_API_BASE_URL` | `http://localhost:8000` | Base URL of the Falcon backend |

The `PUBLIC_` prefix is required by Astro for values that reach browser
bundles. Both readers fall back to `http://localhost:8000`, so dev works with
no `.env` file at all. In production, where Falcon serves the built files
itself, set it to `""` so requests stay same-origin.

**The backend must allow this origin.** Set `FRONTEND_ORIGIN=http://localhost:4321`
in `backend/.env`, or every request fails CORS before it reaches a handler.

---

## Project structure

```text
frontend/
├── src/
│   ├── lib/
│   │   └── api.ts                  The single fetch wrapper — all calls go through it
│   ├── components/                 React islands
│   │   ├── LogoutButton.tsx
│   │   ├── MetricTile.tsx          Live-polling metric tile
│   │   ├── HistoryChart.tsx        Hand-rolled SVG line chart
│   │   ├── CreateContainerForm.tsx
│   │   └── InviteUserForm.tsx
│   └── pages/                      File-based routes
│       ├── index.astro             Landing / session check
│       ├── login.astro
│       ├── admin/
│       │   ├── index.astro         Container list + create form
│       │   └── users.astro         User list + invite form
│       ├── containers/
│       │   └── [id].astro          Container detail + history chart
│       ├── health-check.astro      Temporary — backend connectivity probe
│       ├── metric-tile-test.astro  Throwaway component test page
│       └── history-chart-test.astro  Throwaway component test page
├── astro.config.mjs                Static output + the React integration
├── tsconfig.json                   astro/tsconfigs/strict
└── .env.example
```

---

## The one thing to understand first

**This builds to static output with no SSR adapter**, because the backend
serves the built files and no Node server exists in production
(PROJECT-PLAN decision 7.12).

Astro frontmatter therefore runs at **build time**, where there is no user and
no session cookie. Any authenticated `apiFetch` placed in frontmatter would
`401` during the build and bake that failure permanently into the HTML.

So **every authenticated fetch runs in a `<script>` in the browser**, not in
frontmatter. Each page renders a shell with hidden loading / error / content
states, and the script unhides whichever one applies once the fetch resolves.
Each affected page carries a comment explaining this — see the top of
[src/pages/index.astro](src/pages/index.astro).

The same constraint shapes routing. `containers/[id].astro` is a dynamic route,
and static output requires `getStaticPaths()` to enumerate every path at build
time — when no container UUIDs exist. So it emits one shell at
`/containers/view/` and reads the real id from `?container=<uuid>`. Making
`/containers/<uuid>` a genuine URL requires an SSR adapter.

## Conventions

**All backend calls go through `src/lib/api.ts`.** `apiFetch<T>()` sets
`credentials: "include"` — the session cookies are `httpOnly`, so the browser
must attach them itself; without that line every authenticated call 401s. Non-2xx
responses throw `ApiError`, carrying the status and the backend's own
`description`.

**The server owns validation, and its wording is shown verbatim.** Container
name rules, quota ceilings, duplicate emails — the backend evaluates all of it
and returns prose naming the exact rule broken. Forms display that text as-is
rather than re-implementing the rule, which would create a second copy to drift
out of sync.

**401 redirects, everything else surfaces.** A 401 means "not signed in", so
the page sends you to `/login`. Any other failure is displayed, because
bouncing to a login screen on a 500 or a dead backend hides the real fault.

**Components carry no styles; pages style them.** Islands render plain semantic
markup with class names like `.tile` or `.invite-form`, and each page that
mounts one styles it via `:global(...)` in its own `<style>` block.

**Islands are mounted imperatively when the data set is runtime-only.** Astro's
`client:*` directives resolve at build time, so a list of tiles whose length
depends on a fetch is mounted with `createRoot()` in the page script.
`client:load` is used only where the component set is static, as in
[index.astro](src/pages/index.astro).

**TypeScript is strict, and `any` is not used.** Every component declares an
explicit exported props interface.

---

## What exists today

| Route                                      | What it does                                                                                                                        |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| `/login`                                   | Plain `<a>` to the backend's Google login route — a top-level navigation, not a fetch, since only that can follow the 302 to Google |
| `/`                                        | Session check via `GET /api/auth/me`; shows who you are, plus sign-out                                                              |
| `/admin`                                   | Every container, each with a live `MetricTile`, plus the create form                                                                |
| `/admin/users`                             | All users with role / status / quota columns, plus the invite form                                                                  |
| `/containers/view?container=<uuid>`        | One container's live tile and a history chart with 1h / 24h / 7d windows                                                            |
| `/health-check`                            | Temporary: proves the dev server can reach the backend across the origin boundary                                                   |
| `/metric-tile-test`, `/history-chart-test` | Throwaway pages that render components against hardcoded data, with no backend dependency                                           |

## What is not built yet

Tracked against `docs/AI-AGENT-BUILD-GUIDE.md`:

| Phase                     | Scope                                                   | Status        |
| ------------------------- | ------------------------------------------------------- | ------------- |
| 15 — Scaffold             | Astro project, typed API client                         | Done          |
| 16 — Login / session      | `/login`, landing page, logout                          | Done          |
| 17 — Admin dashboard      | `MetricTile`, container list                            | Done          |
| 18 — Create container     | Form island wired into `/admin`                         | Done          |
| 19 — Detail + history     | `HistoryChart`, container detail page                   | Done          |
| 20.1 — User list + invite | `/admin/users`, `InviteUserForm`                        | Done          |
| 20.2 — Quota + assignment | `UserQuotaEditor`, `AssignmentToggle`                   | **Not built** |
| 21 — Terminal             | `Terminal.tsx` against `POST /api/containers/{id}/exec` | **Not built** |
| 22 — Scoped user view     | Non-admin dashboard                                     | **Not built** |
| 23 — Reliability UX       | `StatusBanner`, loading/error audit                     | **Not built** |

So today the UI can create, inspect, and monitor containers, and invite users —
but editing quotas, granting or revoking container access, promoting or
revoking a user, and the in-browser terminal are all still `curl`-only. The
backend endpoints for every one of them already exist and are tested; only the
UI is missing.

## Known rough edges

- **No navigation between pages.** No step in the build guide added a nav bar,
  so `/admin` and `/admin/users` are reached by typing the URL. `/admin/users`
  and the container detail page each carry a single back-link.
- **Three pages are scheduled for deletion**: `health-check.astro` served its
  purpose in Phase 15, and the two `*-test.astro` pages exist only for the
  steps that built those components. Their own comments say so.
- **`AGENTS.md` and `CLAUDE.md`** are byte-identical AI-agent instruction files
  generated by `npm create astro` (dev-server usage plus links to the Astro
  docs). They are not project documentation and nothing here depends on them.
- **Container detail is not a real URL per container** — see the SSR note above.
