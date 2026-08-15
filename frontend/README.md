# Frontend — Hobby Server Monitor

A modern, high-performance Astro + React web application providing a telemetry console and management control panel for single-host LXD container environments. Communicates with the Falcon backend over HTTP with `httpOnly` session cookies.

---

## Table of Contents

- [Frontend — Hobby Server Monitor](#frontend--hobby-server-monitor)
  - [Table of Contents](#table-of-contents)
  - [1. Architecture \& Design Philosophy](#1-architecture--design-philosophy)
    - [Core Architecture Pillars](#core-architecture-pillars)
  - [2. Features \& User Experience](#2-features--user-experience)
    - [Authentication \& Session Flow](#authentication--session-flow)
    - [Role-Based Access \& Scoped Views](#role-based-access--scoped-views)
    - [Host Capacity \& Container Fleet (`/admin`)](#host-capacity--container-fleet-admin)
    - [User Management \& Quotas (`/admin/users`)](#user-management--quotas-adminusers)
    - [Container Telemetry, Detail \& Interactive Terminal (`/containers/view`)](#container-telemetry-detail--interactive-terminal-containersview)
    - [Reliability \& Degraded Mode UX](#reliability--degraded-mode-ux)
  - [3. Directory \& Component Map](#3-directory--component-map)
  - [4. Design System \& Styling](#4-design-system--styling)
  - [5. Setup \& Local Development](#5-setup--local-development)
    - [Prerequisites](#prerequisites)
    - [Installation \& Launch](#installation--launch)
  - [6. Commands \& Scripts](#6-commands--scripts)
  - [7. Configuration](#7-configuration)
  - [8. Architectural \& Implementation Details](#8-architectural--implementation-details)
    - [Why Static Build with No SSR Adapter](#why-static-build-with-no-ssr-adapter)
    - [Runtime Query Routing on `/containers/view`](#runtime-query-routing-on-containersview)
    - [Dual-Layer Optimistic Session Management](#dual-layer-optimistic-session-management)
    - [Error Handling \& Server-Side Validation Passthrough](#error-handling--server-side-validation-passthrough)
  - [9. Production Deployment](#9-production-deployment)

---

## 1. Architecture & Design Philosophy

The frontend is built using **Astro (Static Output)** coupled with **React Islands** for rich, client-side interactivity:

```text
Browser (Astro Static Shells + React Islands)
   │
   ├─► Session Hydration: sessionStorage (instant paint) + GET /api/auth/me
   ├─► Health Polling (10s): GET /health ──► StatusBanner.tsx
   ├─► Live Metric Polling (5s-10s): GET /api/metrics/latest ──► MetricTile.tsx
   ├─► Telemetry Graphs: GET /api/containers/{id}/history ──► ContainerResourceGraphs.tsx
   ├─► Container Control: PATCH / DELETE /api/containers/{id} ──► ContainerDetailSummary.tsx
   ├─► In-Browser Shell: POST /api/containers/{id}/exec ──► Terminal.tsx
   └─► Admin Operations: /api/users, /api/accounting, /api/lxd/options
```

### Core Architecture Pillars

- **Zero Node Server at Runtime:** Builds to pure static HTML/CSS/JS (`frontend/dist/`), deployable to any web server (or served directly by WSGI / Nginx).
- **Client-Side Data Fetching:** Astro frontmatter executes strictly at build time; all live, user-scoped, and protected backend calls happen in client-side scripts via `apiFetch`.
- **Decoupled Metric Load:** Metric tiles and historical charts poll pre-aggregated points from TinyFlux, creating zero live load on the LXD daemon.
- **Strict Separation of Concerns:** UI islands never invent or duplicate backend validation logic — server-provided validation errors are surfaced directly.

---

## 2. Features & User Experience

### Authentication & Session Flow

- **Google OAuth2 Integration (`/login`):** Direct navigation and popup-based OAuth authentication flow.
- **Zero-Flicker Optimistic Hydration (`src/lib/session.ts`):** Cached session data in `sessionStorage` paints user identity instantly upon navigation; background re-validation against `GET /api/auth/me` ensures token validity.
- **Secure Cookie Management:** Cookies (`access_token`, `refresh_token`) are `httpOnly`, `SameSite=Lax`, and handled automatically by the browser with `credentials: "include"`.
- **Graceful Logout:** `LogoutButton.tsx` issues `POST /api/auth/logout` to revoke server session hashes, clears local storage, and redirects to `/login`.

### Role-Based Access & Scoped Views

- **Admin Users:** Full access to host infrastructure, container fleet provisioning/mutations, user quotas, accounting, and container assignments.
- **Regular Users (`/`):** Scoped landing dashboard showing personal quota consumption bars (RAM, CPU, Disk) against allocated limits, assigned containers, and direct access to instance metrics and terminals.

### Host Capacity & Container Fleet (`/admin`)

- **Capacity Accounting (`AccountingOverview.tsx`):** Real-time display of physical host resources vs committed container allocations (RAM MB, CPU cores, Disk GB) and active container counts.
- **Container Fleet (`MetricTile.tsx`):** Live telemetry tiles for every container on the host with status indicators (`RUNNING`, `STOPPED`, `FROZEN`, `UNKNOWN`), CPU %, RAM meter (used vs peak vs limit), Disk usage, network I/O counters, and IP addresses.
- **Container Provisioning (`CreateContainerForm.tsx`):**
  - Instant validation of container names against LXD naming rules (`^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$`).
  - Image selection (suggestions from `/api/lxd/options` or custom image string).
  - Storage pool & Network bridge selection.
  - Resource limit inputs (RAM in MB, CPU cores, Disk in GB).
  - Autostart and Ephemeral container flags.
  - Immediate user assignment with pre-flight quota validation.

### User Management & Quotas (`/admin/users`)

- **User Invitation (`InviteUserForm.tsx`):** Admin form to invite users by Google email, assigning initial roles (`user` or `admin`) and individual resource quotas.
- **User Management Modal (`UserManageModal.tsx`):**
  - **Account Role & Status (`UserAccountEditor.tsx`):** Change role (`user` / `admin`) and status (`invited`, `active`, `revoked`). Guarded against demoting or revoking the last active admin.
  - **Quota Adjustments (`UserQuotaEditor.tsx`):** Dynamic editing of RAM, CPU, and Disk ceilings (with `0` representing unlimited).
  - **Container Assignment Toggle (`AssignmentToggle.tsx`):** Real-time switch list granting or revoking user access to specific containers with immediate audit logging.

### Container Telemetry, Detail & Interactive Terminal (`/containers/view`)

- **Instance Overview (`ContainerDetailSummary.tsx`):** Breadcrumbs navigation, status pill, architecture, IP addresses, OS description, and container lifecycle actions:
  - **Start / Stop / Restart**
  - **Freeze / Unfreeze**
  - **Delete** (with confirmation dialog)
- **Time-Series Telemetry Graphs (`ContainerResourceGraphs.tsx`, `HistoryChart.tsx`):**
  - Multi-tabbed SVG charts for **RAM Usage**, **CPU Nanoseconds / Load**, **Disk Usage**, and **Network I/O (Rx/Tx)**.
  - Window selector: **1h** (raw samples), **24h** (5-minute downsampled buckets), and **7d** (1-hour downsampled buckets).
  - Hover tooltips with timestamps and exact values.
- **Interactive Shell Terminal (`Terminal.tsx`):**
  - In-browser command execution interface sending JSON argv arrays (`["ls", "-la"]`) to `POST /api/containers/{id}/exec`.
  - Real-time stdout, stderr, and exit code rendering.
  - Terminal command history navigation with Up/Down arrow keys.
  - Preset quick-action command chips (`uptime`, `free -h`, `df -h`, `ps aux`, `ip a`).

### Reliability & Degraded Mode UX

- **Global Status Banner (`StatusBanner.tsx`):** Embedded in `SiteHeader.astro`, polling `GET /health` every 30 seconds. Alerts the user with a dismissible warning banner when the LXD daemon is unreachable or operating in degraded mode.
- **Stale Data Indicators:** Accounting and options forms indicate cached DB figures when live LXD queries fail.
- **Skeleton Shimmer Screens:** Replaces blank screens during network requests with custom animated skeleton loaders across all tables, tiles, and detail views.

---

## 3. Directory & Component Map

```text
frontend/
├── astro.config.mjs               Astro static configuration + React integration
├── package.json                   Engine requirements, scripts, dependencies
├── tsconfig.json                  TypeScript configuration extending astro/tsconfigs/strict
├── dist/                          Static output directory produced by astro build
├── public/
│   ├── favicon.ico
│   └── favicon.svg
└── src/
    ├── lib/
    │   ├── api.ts                 Typed fetch wrapper with credentials & ApiError handling
    │   └── session.ts             Optimistic local storage cache & backend session sync
    ├── styles/
    │   └── global.css             Futuristic design system, CSS tokens, glassmorphism, animations
    ├── components/
    │   ├── AccountingOverview.tsx Real-time host resource vs allocation progress bars
    │   ├── AssignmentToggle.tsx   Toggle switch for user container grants/revocations
    │   ├── ContainerDetailSummary.tsx Instance status, metadata, and lifecycle action buttons
    │   ├── ContainerResourceGraphs.tsx Tabbed multi-metric SVG telemetry graphs (1h, 24h, 7d)
    │   ├── CreateContainerForm.tsx Modal form for provisioning new LXD containers
    │   ├── HistoryChart.tsx       Hand-crafted SVG line and area chart component
    │   ├── InviteUserForm.tsx     Form for inviting new users with custom quotas
    │   ├── LogoutButton.tsx       Sign-out button calling /api/auth/logout
    │   ├── MetricTile.tsx         Live container telemetry card with gauge meters
    │   ├── SiteHeader.astro       Universal role-aware navigation bar & status banner
    │   ├── StatusBanner.tsx       Health monitor banner detecting degraded LXD status
    │   ├── Terminal.tsx           In-browser shell terminal for container execution
    │   ├── UserAccountEditor.tsx  Role and account status editor
    │   ├── UserManageModal.tsx    Modal combining account, quota, and assignment editors
    │   └── UserQuotaEditor.tsx    Resource quota adjustment form
    └── pages/
        ├── index.astro            Landing page & container-user scoped dashboard
        ├── login.astro            Sign-in portal with Google OAuth integration
        ├── admin/
        │   ├── index.astro        Admin container fleet dashboard & creation mount
        │   └── users.astro        User management table & assignment modal mount
        ├── containers/
        │   └── [id].astro         Container detail, telemetry graphs & terminal shell
        ├── health-check.astro     Diagnostic connectivity probe page
        ├── history-chart-test.astro Isolated component preview for HistoryChart
        ├── metric-tile-test.astro Isolated component preview for MetricTile
        └── terminal-test.astro    Isolated component preview for Terminal
```

---

## 4. Design System & Styling

The frontend styles live in [`src/styles/global.css`](src/styles/global.css) and follow a sleek, futuristic UI theme:

- **Typography:**
  - UI Sans: `Inter` (Google Fonts) with fallbacks.
  - Telemetry Mono: `JetBrains Mono` for gauges, quotas, timestamps, metrics, and terminal.
- **Color Palette & Design Tokens:**
  - **Canvas & Surface:** Light subtle canvas (`#f8fafc`), elevated card surfaces with glassmorphism backdrop blurs (`rgba(255, 255, 255, 0.85)`).
  - **Accents:** Electric Cyan (`#0284c7`), Cyber Blue (`#2563eb`), Hyper Purple (`#7c3aed`).
  - **Status Indications:** Running Green (`#059669`), Stopped Red (`#e11d48`), Frozen Amber (`#d97706`), Unknown Violet (`#7c3aed`).
- **Interactive Micro-Animations:**
  - Pulse glow indicators on live telemetry.
  - Shimmer loaders on skeletons.
  - Hover elevation transforms on glass cards.

---

## 5. Setup & Local Development

### Prerequisites

- **Node.js ≥ 22.12.0**
- Backend running on `http://localhost:8000`

### Installation & Launch

```bash
# 1. Navigate to frontend directory
cd frontend

# 2. Install dependencies
npm install

# 3. Configure environment (optional in development; defaults to localhost:8000)
cp .env.example .env

# 4. Start Astro development server with HMR
npm run dev
# Server running at http://localhost:4321
```

---

## 6. Commands & Scripts

| Command           | Description                                                               |
| ----------------- | ------------------------------------------------------------------------- |
| `npm run dev`     | Starts development server with Hot Module Replacement at `localhost:4321` |
| `npm run build`   | Compiles static production build into `./dist/`                           |
| `npm run preview` | Serves production `./dist/` bundle locally for previewing                 |
| `npx astro check` | Runs full TypeScript and Astro template type-checking                     |

---

## 7. Configuration

Environment variables are read via Vite/Astro `import.meta.env`:

| Variable              | Default                 | Purpose                                                                |
| --------------------- | ----------------------- | ---------------------------------------------------------------------- |
| `PUBLIC_API_BASE_URL` | `http://localhost:8000` | Base URL of the Falcon backend. In same-origin production, set to `""` |

> [!IMPORTANT]
> The backend's `FRONTEND_ORIGIN` in `backend/.env` must match the URL where this frontend is served (e.g. `http://localhost:4321` in development). Otherwise, browser CORS policies will block cookie-bearing requests.

---

## 8. Architectural & Implementation Details

### Why Static Build with No SSR Adapter

In production, the entire system is hosted on a single Linux host without needing a separate Node.js server process. By configuring Astro for static output (`output: "static"` in `astro.config.mjs`), Astro emits pure static assets into `dist/`. The Falcon API or an Nginx reverse proxy serves these files directly, minimizing resource consumption.

### Runtime Query Routing on `/containers/view`

Because static site generation creates pages at build time, Astro cannot know container UUIDs in advance. [`src/pages/containers/[id].astro`](src/pages/containers/%5Bid%5D.astro) uses `getStaticPaths()` returning a sentinel path (`/containers/view/`). At runtime, the client script reads the container ID from `?container=<uuid>` and dynamically fetches container metadata.

### Dual-Layer Optimistic Session Management

1. **Synchronous Hydration:** When a user navigates between pages, `syncSession()` in [`src/lib/session.ts`](src/lib/session.ts) immediately reads the session from `sessionStorage`, updating the UI header and greeting with zero layout shift or visual flicker.
2. **Authoritative Revalidation:** Concurrently, an asynchronous request is dispatched to `GET /api/auth/me`. If the backend returns 401 (e.g., token expired or revoked), the local cache is cleared and the browser redirects to `/login`.

### Error Handling & Server-Side Validation Passthrough

[`src/lib/api.ts`](src/lib/api.ts) defines an `ApiError` class that captures the HTTP status and the backend's exact `title` and `description`. Forms display the server's precise error message (e.g. quota excess numbers or invalid container name regex rules) verbatim, ensuring the frontend never drifts from backend policy.

---

## 9. Production Deployment

When building for production:

```bash
cd frontend
npm run build
```

This generates static HTML, JS, and CSS files in `frontend/dist/`. In a standard deployment:

- Place the `dist/` directory behind your reverse proxy (e.g., Nginx, Caddy), or configure Falcon to serve static assets.
- Ensure the backend's `FRONTEND_ORIGIN` matches the production domain.
- Ensure `SESSION_COOKIE_SECURE=true` in `backend/.env` when serving over HTTPS.
