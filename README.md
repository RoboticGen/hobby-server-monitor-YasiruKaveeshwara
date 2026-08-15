# Hobby Server Monitor

A modern, lightweight, single-host telemetry console and container management control panel for Linux systems running LXD. Designed for hobby servers, home labs, and small development teams.

---

## Highlights

- **Lightweight & High Efficiency:** Consumes only ~100 MB RAM and <0.5% CPU combined across all backend processes at idle.
- **Decoupled Metric Architecture:** An independent background collector records time-series metrics into TinyFlux TSDB; dashboard viewing adds zero live polling load to LXD.
- **Multi-Tenant Access & Quotas:** Administers user accounts, enforces CPU/RAM limits via LXD, accounts Disk against per-user quotas, and assigns container access with zero cross-tenant leakage.
- **Injection-Safe Web Terminal:** In-browser interactive shell executing JSON argv arrays directly without shell string interpolation.
- **Real-Time & Historical Telemetry:** Live gauge tiles and multi-tier historical SVG charts across 1h, 24h, and 7d windows.
- **Zero Node Server in Production:** Frontend compiles to pure static HTML/JS/CSS via Astro and React islands, deployable behind any web server or WSGI gateway.
- **Production Systemd Units:** Pre-configured systemd service templates with automatic crash recovery running under a dedicated low-privilege `hsm-runner` user.
- **Comprehensive Test Suite:** 100% verified test suite with **378 passed tests** covering unit, integration, authentication, resilience, and security invariants.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Feature Walkthrough](#2-feature-walkthrough)
3. [Quickstart & Local Setup](#3-quickstart--local-setup)
4. [Production Deployment](#4-production-deployment)
5. [Data Model](#5-data-model)
   - [SQLite Schema](#sqlite-schema)
   - [TinyFlux Time-Series TSDB](#tinyflux-time-series-tsdb)
6. [API Reference](#6-api-reference)
7. [Security Architecture](#7-security-architecture)
8. [Configuration Reference](#8-configuration-reference)
9. [Testing & Verification](#9-testing--verification)

---

## 1. Architecture Overview

The system is organized into two independent long-running backend processes and a static Astro/React frontend:

```text
┌────────────────────────────────────────────────────────────────────────┐
│               Browser (Astro Static Shell + React Islands)              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTPS + httpOnly Cookie Session
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│               Falcon WSGI API Server (waitress-serve)                  │
│                                                                        │
│   ├── AuthMiddleware (JWT verification & RBAC context)                 │
│   ├── Resource Handlers (Validation, Quota checks, Audit logs)         │
│   └── LXD Client Wrapper (pylxd containment layer)                     │
└───────────┬───────────────────────┬─────────────────────────┬──────────┘
            │                       │                         │
            ▼                       ▼                         ▼
┌──────────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
│  SQLite (app.db)     │ │ TinyFlux (TSDB)      │ │     LXD Daemon       │
│  - Users & Quotas    │ │ - Historical metrics │ │  (unix.socket / TLS) │
│  - Sessions & Tokens │ │ - Rollups (5m / 1h)  │ │  - Containers        │
│  - Container State   │ └──────────▲───────────┘ │  - Live state & exec │
│  - Assignments       │            │             └──────────▲───────────┘
│  - Audit Log         │            │ (writes metric samples)│ (polls state)
└──────────────────────┘ ┌──────────┴────────────────────────┴───────────┐
                         │    Background Metric Collector Daemon         │
                         │    - Polls LXD every 5s                       │
                         │    - Hourly multi-tier downsampling & pruning │
                         └───────────────────────────────────────────────┘
```

### Process Isolation Matrix

| Process       | Production Entrypoint                                      | Role & Invariants                                                                    |
| ------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| **API**       | `waitress-serve --port=8000 --call backend.app:create_app` | Serves HTTP API requests, handles auth, performs mutations; never polls LXD for TSDB |
| **Collector** | `python -m backend.collector.collector`                    | Independent polling loop; writes TinyFlux samples and runs hourly rollup pruning     |
| **Frontend**  | Static files in `frontend/dist/`                           | Served directly by web server; performs client-side data fetching via `apiFetch`     |

> [!NOTE]
> **Privilege Boundary:** The only file in the backend that imports `pylxd` is `backend/lxd/client.py`. All other modules reach LXD through that module's functions, keeping hypervisor interactions auditable in a single location.

---

## 2. Feature Walkthrough

### 🚀 Host Capacity & Container Fleet (`/admin`)

- **Capacity Accounting:** Real-time host hardware capacity vs committed container allocations (RAM, CPU cores, Disk) with stale-indicator support during hypervisor outages.
- **Telemetry Metric Tiles:** Live container cards polling `/api/metrics/latest` with status badges (`RUNNING`, `STOPPED`, `FROZEN`, `UNKNOWN`), CPU %, RAM meter (used vs peak vs limit), Disk usage, network I/O rates, and IP addresses.
- **Container Provisioning:** Form validation against LXD naming conventions (`^[a-z0-9-]+$`), image selection, storage pool / network bridge configuration, resource limits, and pre-flight user quota validation.
- **Container Lifecycle Actions:** Start, Stop, Restart, Freeze, Unfreeze, and Soft-Delete with confirmation dialogs.

### 💻 In-Browser Terminal (`/containers/view?container=<id>`)

- Interactive command execution against `POST /api/containers/{id}/exec`.
- Command history navigation using Up/Down arrow keys.
- Quick-action preset command buttons (`uptime`, `free -h`, `df -h`, `ps aux`, `ip a`).
- Injection-safe execution: inputs are strictly received and forwarded as JSON string arrays without shell interpolation.

### 📈 Historical Telemetry Charts

- Responsive, custom SVG line and area charts (`ContainerResourceGraphs.tsx`, `HistoryChart.tsx`).
- Multi-metric tabs: **RAM Usage**, **CPU Nanoseconds / Load**, **Disk Usage**, and **Network I/O (Rx/Tx)**.
- Selectable time windows: **1h** (raw 5s points), **24h** (5-minute rollups), and **7d** (1-hour rollups).

### 👥 User Administration & Quota Ceilings (`/admin/users`)

- Google OAuth2 email invitations with initial role (`admin` or `user`) and individual resource quotas (RAM MB, CPU cores, Disk GB).
- Modal editor (`UserManageModal.tsx`) combining account status (`invited`, `active`, `revoked`), role modification, quota editing, and real-time container assignment switches.
- Strict last-admin protection preventing demotion or revocation of the final active admin.

### 👤 Scoped Non-Admin Dashboard (`/`)

- Non-admin users see only containers explicitly assigned to them.
- Live personal quota meters displaying RAM, CPU, and Disk usage vs granted limits.
- Direct links to container telemetry and terminal shells.

---

## 3. Quickstart & Local Setup

Follow these steps on an **Ubuntu 22.04+** machine or **WSL2 (Ubuntu)**.

### 3.1 Prerequisites

- Linux OS with `systemd` and `snapd`
- Python 3.10+ and `python3-venv`
- Node.js ≥ 22.12.0 and `npm`
- LXD 5.x installed:
  ```bash
  sudo snap install lxd
  ```
- Google Cloud Project with OAuth 2.0 Credentials

### 3.2 Initialize LXD

```bash
# Minimal non-interactive LXD configuration
sudo lxd init --minimal

# Add your current user to the lxd group
sudo usermod -aG lxd $USER
newgrp lxd
```

### 3.3 Google OAuth Setup

1. Open [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Credentials**.
2. Create an **OAuth 2.0 Client ID** (Application type: _Web application_).
3. Add `http://localhost:8000/api/auth/google/callback` to **Authorized redirect URIs**.
4. Note your **Client ID** and **Client Secret**.

### 3.4 Clone and Configure

```bash
git clone <your-repo-url>
cd hobby-server-monitor

# Create and activate Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install backend dependencies
pip install -r backend/requirements.txt

# Configure backend environment
cp backend/.env.example backend/.env
# Edit backend/.env with your Google OAuth credentials and ADMIN_BOOTSTRAP_EMAIL
```

### 3.5 Initialize the Database

```bash
python -m backend.db.init_db
```

This creates the SQLite schema in `data/app.db`. The command is idempotent and safe to re-run.

### 3.6 Run the Application Locally

Open three terminal tabs:

**Terminal 1 — API Server:**

```bash
source .venv/bin/activate
python backend/app.py
# Listens on http://localhost:8000
```

**Terminal 2 — Metric Collector:**

```bash
source .venv/bin/activate
python -m backend.collector.collector
# Polls LXD every 5s and writes to data/metrics.tinyflux
```

**Terminal 3 — Frontend Dev Server:**

```bash
cd frontend
npm install
npm run dev
# Listens on http://localhost:4321
```

### 3.7 First Sign-In (Bootstrap Flow)

1. Navigate to `http://localhost:4321`.
2. Click **Sign in with Google** and authenticate using the email configured as `ADMIN_BOOTSTRAP_EMAIL` in `backend/.env`.
3. That account is automatically created as the initial **Admin**. All subsequent users must be invited by an admin before they can log in.

---

## 4. Production Deployment

In production, both backend processes run as `systemd` services under a dedicated low-privilege `hsm-runner` user in the `lxd` group.

```bash
# 1. Create dedicated system user
sudo useradd --system --no-create-home hsm-runner
sudo usermod -aG lxd hsm-runner

# 2. Grant data directory permissions
mkdir -p data
sudo chown -R hsm-runner:hsm-runner data/

# 3. Install and start systemd units
sudo cp deploy/hsm-api.service /etc/systemd/system/
sudo cp deploy/hsm-collector.service /etc/systemd/system/
sudo sed -i "s|/opt/hobby-server-monitor|$(pwd)|g" /etc/systemd/system/hsm-*.service
sudo systemctl daemon-reload
sudo systemctl enable --now hsm-api.service hsm-collector.service

# 4. Build frontend static bundle
cd frontend
npm install
npm run build
# Deploy frontend/dist/ behind your reverse proxy (Nginx / Caddy)
```

> [!TIP]
> See [`deploy/notes.md`](deploy/notes.md) for detailed deployment steps, reverse proxy configurations, and idle resource footprint profiling using [`scripts/measure_footprint.sh`](scripts/measure_footprint.sh).

---

## 5. Data Model

### SQLite Schema

The relational database (`data/app.db`) uses parameterized queries in `backend/db/repo.py`:

```sql
-- Users and quota ceilings
CREATE TABLE users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('admin','user')),
    status        TEXT NOT NULL CHECK(status IN ('invited','active','revoked')),
    quota_ram_mb  INTEGER NOT NULL DEFAULT 0,  -- 0 = unlimited
    quota_cpu     REAL    NOT NULL DEFAULT 0,
    quota_disk_gb INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

-- httpOnly session tokens (revocation deletes row server-side)
CREATE TABLE sessions (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES users(id),
    refresh_token_hash TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

-- Containers tracked by persistent UUID
CREATE TABLE containers (
    id            TEXT PRIMARY KEY,        -- our uuid, stable across renames
    lxd_name      TEXT UNIQUE NOT NULL,    -- current LXD-side name
    image         TEXT NOT NULL,
    created_by    TEXT NOT NULL REFERENCES users(id),
    limit_ram_mb  INTEGER NOT NULL DEFAULT 0,  -- DB-cached RAM limit in MB
    limit_cpu     REAL    NOT NULL DEFAULT 0,  -- DB-cached CPU core count
    limit_disk_gb INTEGER NOT NULL DEFAULT 0,  -- DB-cached disk limit in GB
    deleted_at    TEXT,                    -- Soft-delete timestamp; NULL = active
    created_at    TEXT NOT NULL
);

-- Container user access grants
CREATE TABLE assignments (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    container_id TEXT NOT NULL REFERENCES containers(id),
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

-- Immutable audit log
CREATE TABLE audit_log (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);
```

### TinyFlux Time-Series TSDB

Metrics are recorded in `data/metrics.tinyflux` using a multi-resolution layout:

| Attribute      | Description                                                                                               |
| -------------- | --------------------------------------------------------------------------------------------------------- |
| **Tags**       | `container_id` (UUID), `resolution` (`raw` / `5m` / `1h`)                                                 |
| **Fields**     | `cpu_usage_ns`, `ram_used_mb`, `ram_peak_mb`, `disk_used_mb`, `net_rx_bytes`, `net_tx_bytes`, `pid_count` |
| **Timestamps** | UTC ISO-8601 sample timestamps                                                                            |

#### Retention & Downsampling Pipeline

1. **Raw Tier:** 5-second samples kept for **24 hours**.
2. **5-Minute Tier:** Hourly rollup collapses raw points into 5-minute buckets (`resolution=5m`) kept for **7 days**.
3. **1-Hour Tier:** Hourly rollup collapses those into 1-hour buckets (`resolution=1h`) kept for **90 days**.
   Each bucket is reduced per field, not uniformly averaged: gauges take the mean, the monotonic counters (`cpu_usage_ns`, `net_rx_bytes`, `net_tx_bytes`) keep the bucket's last reading so rate charts stay exact, and `ram_peak_mb` takes the max. See REPORT §7.6.
4. **Pruning:** Points older than 90 days are deleted outright, keeping disk growth strictly bounded.

---

## 6. API Reference

All endpoints are hosted under `http://<host>:<PORT>`.

| Method   | Path                                             | Access Level                 | Description                                                              |
| -------- | ------------------------------------------------ | ---------------------------- | ------------------------------------------------------------------------ |
| `GET`    | `/health`                                        | Public                       | System health and LXD daemon reachability check (`lxd_reachable: bool`)  |
| `GET`    | `/api/auth/google/login`                         | Public                       | Generates Google OAuth consent screen redirect URL                       |
| `GET`    | `/api/auth/google/callback`                      | Public                       | Handles OAuth code exchange, user provisioning, and session cookies      |
| `GET`    | `/api/auth/me`                                   | Authenticated                | Returns user identity, role, and current resource allocations vs quota   |
| `POST`   | `/api/auth/refresh`                              | Valid refresh cookie         | Reissues the access token; re-reads role/status so revocation applies    |
| `POST`   | `/api/auth/logout`                               | Authenticated                | Revokes session server-side and clears client cookies                    |
| `GET`    | `/api/containers`                                | User (Assigned); Admin (All) | Lists containers with live LXD state                                     |
| `POST`   | `/api/containers`                                | Admin Only                   | Provisions a container with bounds and quota enforcement                 |
| `GET`    | `/api/containers/{id}`                           | User (Assigned); Admin (All) | Returns enriched container metadata and network IP addresses             |
| `PATCH`  | `/api/containers/{id}`                           | Admin Only                   | Mutates lifecycle state (start/stop/restart/freeze) or resource limits   |
| `DELETE` | `/api/containers/{id}`                           | Admin Only                   | Soft-deletes container and marks record deleted in SQLite                |
| `GET`    | `/api/containers/{id}/history`                   | User (Assigned); Admin (All) | Queries time-series points (`window=1h\|24h\|7d`) from TinyFlux          |
| `POST`   | `/api/containers/{id}/exec`                      | User (Assigned); Admin (All) | Executes command; body must be `{"command": ["arg0", "arg1", ...]}`      |
| `GET`    | `/api/metrics/latest`                            | User (Assigned); Admin (All) | Retrieves newest raw sample for container (never polls LXD live)         |
| `GET`    | `/api/metrics/recent`                            | User (Assigned); Admin (All) | Retrieves last N raw samples for chart pre-seeding                       |
| `GET`    | `/api/users`                                     | Admin Only                   | Lists all registered users and quota allocations                         |
| `POST`   | `/api/users`                                     | Admin Only                   | Invites a user by Google email with assigned role and quotas             |
| `PATCH`  | `/api/users/{id}`                                | Admin Only                   | Updates user role, status (`active`/`revoked`), or quota limits          |
| `POST`   | `/api/users/{user_id}/containers/{container_id}` | Admin Only                   | Grants container access with transactional quota check                   |
| `DELETE` | `/api/users/{user_id}/containers/{container_id}` | Admin Only                   | Revokes container access                                                 |
| `GET`    | `/api/accounting`                                | Admin Only                   | Returns host physical totals and per-user allocation vs quota breakdowns |
| `GET`    | `/api/lxd/options`                               | Admin Only                   | Returns available storage pools, networks, and image aliases             |

---

## 7. Security Architecture

### Authentication & Cookies (Decision 7.1)

- Issues short-lived JWT access tokens (configurable, default 30m) and long-lived refresh tokens (7 days).
- Tokens are stored exclusively in `httpOnly`, `Secure`, `SameSite=Lax` cookies — never exposed to client-side `localStorage`.
- The SHA-256 hash of each refresh token is stored in SQLite; server-side session deletion constitutes real logout.
- The OAuth flow carries a random `state` value, echoed by Google and compared against a short-lived `httpOnly` cookie before the authorization code is exchanged. This blocks login CSRF, where an attacker's own authorization code is fed to a victim's browser to silently sign them into the attacker's account.

### Authorization & RBAC (Decision 7.2)

- Central `AuthMiddleware` verifies JWT tokens and populates `req.context.user`.
- Handlers invoke explicit authorization helpers (`require_role("admin")`, `require_container_access(user, container_id)`).
- Requesting an unassigned container returns **403 Forbidden, not 404 Not Found**, preventing container ID enumeration attacks.

### Admin Bootstrap (Decision 7.11)

- The initial admin is defined strictly via `ADMIN_BOOTSTRAP_EMAIL` in `.env`.
- Uninvited Google accounts receiving authentication callbacks receive an HTTP 403 response.

### Terminal Injection Prevention (Decision 7.7)

- `POST /api/containers/{id}/exec` accepts commands exclusively as a JSON array of strings (`["uptime"]`).
- String commands (`{"command": "rm -rf /"}`) are rejected with HTTP 400.
- Arrays are passed directly to `pylxd`'s exec execution call without shell string interpolation.

### Unprivileged Containers (Decision 7.4)

- All container creation requests enforce `security.privileged=false` server-side.
- The backend and collector services execute as the low-privilege `hsm-runner` account.

---

## 8. Configuration Reference

All backend configuration is read in `backend/config.py`. Copy `backend/.env.example` to `backend/.env`:

| Variable                        | Required | Default                                       | Purpose                                                                    |
| ------------------------------- | :------: | --------------------------------------------- | -------------------------------------------------------------------------- |
| `GOOGLE_CLIENT_ID`              |    ✅    | —                                             | OAuth 2.0 Client ID from Google Cloud Console                              |
| `GOOGLE_CLIENT_SECRET`          |    ✅    | —                                             | OAuth 2.0 Client Secret                                                    |
| `GOOGLE_REDIRECT_URI`           |    ✅    | —                                             | OAuth callback URL (e.g. `http://localhost:8000/api/auth/google/callback`) |
| `JWT_SECRET`                    |    ✅    | —                                             | 256-bit random secret used for JWT HMAC signing                            |
| `ADMIN_BOOTSTRAP_EMAIL`         |    ✅    | —                                             | The single email address granted Admin status on first sign-in             |
| `DATABASE_PATH`                 |    ✅    | `./data/app.db`                               | Filesystem path to the SQLite database                                     |
| `TINYFLUX_PATH`                 |    ✅    | `./data/metrics.tinyflux`                     | Filesystem path to the TinyFlux TSDB file                                  |
| `LXD_ENDPOINT`                  |    ✅    | `unix:///var/snap/lxd/common/lxd/unix.socket` | Unix socket path or remote HTTPS endpoint for LXD                          |
| `LXD_CERT_PATH`                 |    ❌    | —                                             | Client TLS certificate (only required for remote HTTPS LXD)                |
| `LXD_KEY_PATH`                  |    ❌    | —                                             | Client TLS key (only required for remote HTTPS LXD)                        |
| `COLLECTOR_INTERVAL_SECONDS`    |    ❌    | `10`                                          | Polling cycle interval in seconds for metric collector                     |
| `FRONTEND_ORIGIN`               |    ✅    | `http://localhost:4321`                       | Allowed CORS origin and cookie domain for browser requests                 |
| `ACCESS_TOKEN_LIFETIME_MINUTES` |    ❌    | `30`                                          | Access token JWT validity duration in minutes                              |
| `SESSION_COOKIE_SECURE`         |    ❌    | `false`                                       | Set `true` in production when serving over HTTPS                           |
| `PORT`                          |    ❌    | `8000`                                        | Port the Falcon WSGI API listens on                                        |

---

## 9. Testing & Verification

### Run Backend Test Suite

```bash
# Run all 383 backend tests
.venv/bin/pytest backend -q

# Run with verbose output and test execution timings
.venv/bin/pytest backend -v --durations=10
```

**Results:** `378 passed, 4 skipped, 1 xfailed, 0 failed` in ~3.9s.

> [!NOTE]
> See [`backend/README.md` §14 Testing](backend/README.md#14-testing) for the suite layout, the fake-LXD harness, the integration-defect post-mortems, and why the one xfail is pinned rather than fixed.

### Run Frontend Diagnostics

```bash
cd frontend

# Verify TypeScript and Astro template types
npx astro check

# Build static production bundle
npm run build
```

**Results:** `0 errors, 0 warnings`, 9 static pages compiled into `frontend/dist/`.
