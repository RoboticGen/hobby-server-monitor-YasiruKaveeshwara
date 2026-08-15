# Backend — Hobby Server Monitor

A Falcon (WSGI) API and a standalone metrics collector for managing and
monitoring LXD containers on a single host.

The backend is responsible for everything the browser is not trusted with:
Google sign-in, session issuance, role and per-container authorization, LXD
mutation, quota enforcement, metric collection, and retention. The frontend is
a static Astro build that talks to this API over CORS with cookies.

---

## Contents

- [Backend — Hobby Server Monitor](#backend--hobby-server-monitor)
  - [Contents](#contents)
  - [1. Architecture](#1-architecture)
  - [2. Technology choices](#2-technology-choices)
  - [3. Directory map](#3-directory-map)
  - [4. Setup](#4-setup)
    - [Google OAuth setup](#google-oauth-setup)
    - [LXD prerequisites](#lxd-prerequisites)
  - [5. Configuration](#5-configuration)
    - [Required](#required)
    - [Optional](#optional)
  - [6. Running](#6-running)
    - [API (development)](#api-development)
    - [API (production)](#api-production)
    - [Collector](#collector)
    - [Smoke check](#smoke-check)
  - [7. API reference](#7-api-reference)
    - [Selected request bodies](#selected-request-bodies)
    - [Status codes used](#status-codes-used)
  - [8. Data model](#8-data-model)
    - [SQLite (`DATABASE_PATH`)](#sqlite-database_path)
    - [TinyFlux (`TINYFLUX_PATH`)](#tinyflux-tinyflux_path)
  - [9. Authentication and authorization](#9-authentication-and-authorization)
    - [Sign-in flow](#sign-in-flow)
    - [Tokens and cookies](#tokens-and-cookies)
    - [Enforcement](#enforcement)
    - [CORS](#cors)
  - [10. Quotas](#10-quotas)
    - [Where quota is enforced](#where-quota-is-enforced)
    - [Concurrency](#concurrency)
  - [11. Metrics pipeline and retention](#11-metrics-pipeline-and-retention)
    - [Collection](#collection)
    - [Retention](#retention)
      - [How a bucket is reduced](#how-a-bucket-is-reduced)
    - [Serving](#serving)
  - [12. Error handling conventions](#12-error-handling-conventions)
  - [13. Security decisions](#13-security-decisions)
  - [14. Testing](#14-testing)
    - [Layout](#layout)
    - [Two levels, two stub depths](#two-levels-two-stub-depths)
    - [The route-coverage inversion](#the-route-coverage-inversion)
    - [The expected xfail](#the-expected-xfail)
    - [Recorded results](#recorded-results)
  - [15. Deployment](#15-deployment)
  - [16. Known limitations](#16-known-limitations)

---

## 1. Architecture

Two **separate operating-system processes**, not one process with a background
thread:

```text
                    ┌──────────────────────────────────────┐
   browser ──CORS──▶│  API process   (backend/app.py)      │
   (cookies)        │  falcon.App under waitress/wsgiref   │
                    └───┬──────────────┬───────────────┬───┘
                        │              │               │
                  reads/writes      reads only      mutates
                        │              │               │
                    ┌───▼────┐    ┌────▼─────┐    ┌────▼────┐
                    │ SQLite │    │ TinyFlux │    │   LXD   │
                    │ app.db │    │ metrics  │    │ daemon  │
                    └───▲────┘    └────▲─────┘    └────▲────┘
                        │              │               │
                   reads container     │ writes        │ polls state
                   list                │               │
                    ┌───┴──────────────┴───────────────┴───┐
                    │  Collector process                   │
                    │  (backend/collector/collector.py)     │
                    └──────────────────────────────────────┘
```

**Why two processes.** The brief requires that the metrics collector run
independently of the UI/API. Killing, restarting, or redeploying the API must
not create a gap in the metric history, and a collector stuck on a slow LXD
call must not consume an API worker. `python -m backend.collector.collector`
is a genuinely separate process with its own lifecycle.

**Who talks to LXD.** Only the API process (on user-initiated mutations) and
the collector (on its fixed timer). The metrics endpoints deliberately never
call LXD — see [§11](#11-metrics-pipeline-and-retention).

**Single points of import.** Two rules make the codebase auditable by reading
one file each:

| Concern              | The only module allowed to do it |
| -------------------- | -------------------------------- |
| Reading `os.environ` | `backend/config.py`              |
| Importing `pylxd`    | `backend/lxd/client.py`          |

A reviewer can open `lxd/client.py` and see the complete set of operations this
application can perform against the host's LXD daemon, without grepping.
Likewise every configuration value is declared, typed, and validated in one
dataclass.

---

## 2. Technology choices

| Layer             | Choice                         | Notes                                                                                 |
| ----------------- | ------------------------------ | ------------------------------------------------------------------------------------- |
| HTTP framework    | **Falcon** ≥ 4.0               | WSGI, no ORM, no magic routing. Resources are plain classes with `on_get`/`on_post`/… |
| Relational store  | **SQLite** (stdlib `sqlite3`)  | Single-host app; one file, no server to operate                                       |
| Time-series store | **TinyFlux** `==1.2.0`         | CSV-backed, tag-queryable. Version **pinned** — see below                             |
| LXD access        | **pylxd**                      | Unix socket by default, HTTPS + client cert supported                                 |
| Tokens            | **PyJWT** (HS256)              | Access tokens only; refresh tokens are opaque random strings                          |
| OAuth             | **google-auth** + **requests** | ID-token signature verification against Google's JWKS                                 |
| Config            | **python-dotenv**              | `.env` for dev; real env vars always win                                              |
| Production server | **waitress**                   | Pure-Python WSGI server, no C build deps                                              |
| Tests             | **pytest**                     | Unit + integration, see [§14](#14-testing)                                            |

**Why TinyFlux is pinned rather than floated.** In 1.2.0, inserting a point
older than the newest one already stored correctly invalidates the in-memory
time index — but a subsequent `remove()` rebuilds that index from a partial
view and marks it valid again. From then on, time-range queries in that process
are answered from an index that does not match the file. That is exactly the
sequence retention performs (write a back-dated rollup bucket, then delete the
raw points it replaced), so `tsdb/store.py:remove_points()` works around it by
forcing `store.index.invalidate()` after every deletion. An upgrade could fix
the defect (making the workaround harmless but pointless) or change its shape
(making it wrong), so the version moves only alongside the characterisation
test in `tests/integration/test_pipeline.py::TestTinyFluxIndexBehaviour`.

---

## 3. Directory map

```text
backend/
├── app.py                      Falcon app factory + the complete route table
├── config.py                   The only module that reads os.environ
├── requirements.txt
├── .env.example                Documented template — copy to .env
│
├── auth/
│   ├── jwt_utils.py            Access-token sign/verify; refresh-token mint/hash
│   ├── middleware.py           AuthMiddleware + require_role / require_container_access
│   └── oauth.py                Google auth URL, code exchange, ID-token verification
│
├── db/
│   ├── schema.sql              The five tables, applied once by init_db
│   ├── init_db.py              Idempotent database creation
│   └── repo.py                 Every SQL statement in the codebase
│
├── lxd/
│   ├── client.py               The only module that imports pylxd
│   └── quota.py                Allocation computation and quota checks
│
├── tsdb/
│   └── store.py                The only module that imports tinyflux
│
├── collector/
│   ├── collector.py            Standalone process: poll LXD → write TinyFlux
│   └── retention.py            Three-tier downsample + prune, run hourly
│
├── resources/                  One module per HTTP concern
│   ├── health.py               GET /health
│   ├── auth.py                 Google login/callback, /me, logout
│   ├── containers.py           Container list/create/patch/delete, LXD options
│   ├── assignments.py          Grant/revoke + container detail (403-not-404)
│   ├── users.py                Invite/list/update users
│   ├── metrics.py              Latest sample + windowed history
│   ├── terminal.py             POST exec into a container
│   ├── accounting.py           Host capacity vs allocation vs quota
│   └── validation.py           Request-body type guards (400, not 500)
│
└── tests/
    ├── conftest.py             Shared temp DB, created before backend imports
    ├── test_*.py               10 unit suites (wrapper-level LXD stubs)
    └── integration/
        ├── conftest.py         Isolated SQLite + TinyFlux, JWT minting
        ├── fake_lxd.py         In-memory pylxd double with outage simulation
        └── test_*.py           6 integration suites through the real app
```

---

## 4. Setup

Run everything from the **repository root** — the code is imported as the
`backend` package (`python -m backend.app`), not run as loose scripts.

```bash
# 1. Virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Dependencies
pip install -r backend/requirements.txt

# 3. Configuration
cp backend/.env.example backend/.env
#    then edit backend/.env — see §5

# 4. Create the database (idempotent; refuses to touch an existing file)
python -m backend.db.init_db
```

`init_db` is deliberately idempotent: if the database file already exists it
prints a notice and returns without modifying anything. Re-running the schema
DDL against a live database would either fail or, worse, silently drop and
recreate tables — unacceptable for a file that may hold real user data.

### Google OAuth setup

1. Create an OAuth 2.0 Client ID (type: _Web application_) in the Google Cloud
   Console.
2. Add an **Authorized redirect URI** that matches `GOOGLE_REDIRECT_URI`
   exactly, character for character — e.g.
   `http://localhost:8000/api/auth/google/callback`.
3. Copy the client ID and secret into `backend/.env`.
4. Set `ADMIN_BOOTSTRAP_EMAIL` to the Google account that should become the
   first admin.

### LXD prerequisites

The account running the API and collector must be able to reach the LXD socket
(typically membership of the `lxd` group). Verify with `lxc list` before
starting the backend; `GET /health` will report `lxd_reachable: false` if it
cannot connect.

---

## 5. Configuration

Every variable below is read in exactly one place: `backend/config.py`. See
`backend/.env.example` for the annotated template.

`config.py` is **fail-fast**: a missing or empty required variable raises
`RuntimeError` at import time, before the app serves its first request. This is
a security property, not a convenience — an empty `JWT_SECRET` would let anyone
forge valid tokens, and a missing `ADMIN_BOOTSTRAP_EMAIL` would mean no admin
account could ever be created.

### Required

| Variable                | Type | Purpose                                                                                     |
| ----------------------- | ---- | ------------------------------------------------------------------------------------------- |
| `GOOGLE_CLIENT_ID`      | str  | OAuth client ID from Google Cloud Console                                                   |
| `GOOGLE_CLIENT_SECRET`  | str  | OAuth client secret                                                                         |
| `GOOGLE_REDIRECT_URI`   | str  | Callback URL; must match Google's registration exactly                                      |
| `JWT_SECRET`            | str  | HS256 signing key. Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_BOOTSTRAP_EMAIL` | str  | The one email that becomes admin on first sign-in                                           |
| `DATABASE_PATH`         | path | SQLite file, e.g. `./data/app.db`                                                           |
| `TINYFLUX_PATH`         | path | TinyFlux file, e.g. `./data/metrics.tinyflux`                                               |
| `LXD_ENDPOINT`          | str  | e.g. `unix:///var/snap/lxd/common/lxd/unix.socket`                                          |
| `FRONTEND_ORIGIN`       | str  | Single allowed CORS origin, e.g. `http://localhost:4321`                                    |

### Optional

| Variable                        | Default | Purpose                                                               |
| ------------------------------- | ------- | --------------------------------------------------------------------- |
| `LXD_CERT_PATH`                 | `""`    | Client cert for a remote HTTPS LXD endpoint                           |
| `LXD_KEY_PATH`                  | `""`    | Client key. Both must be set to switch off unix-socket mode           |
| `COLLECTOR_INTERVAL_SECONDS`    | `5`     | Seconds between collection cycles (default: 5s)                       |
| `ACCESS_TOKEN_LIFETIME_MINUTES` | `30`    | Access token lifetime in minutes before re-authentication is required |
| `SESSION_COOKIE_SECURE`         | `false` | Set `true` in production (requires HTTPS)                             |
| `PORT`                          | `8000`  | Port the dev server binds                                             |

**Precedence.** `load_dotenv()` is called _without_ `override`, so a real
environment variable always beats the `.env` file. A systemd unit using
`EnvironmentFile=` therefore wins over a stale `.env` left on the box.

> `backend/.env` holds live secrets and must never be committed or pasted into
> issues, logs, or chat. Only `.env.example` — which contains placeholders — is
> tracked.

---

## 6. Running

Both processes are started from the repository root with the virtualenv active.

### API (development)

```bash
python -m backend.app
# [dev] Starting Falcon on 0.0.0.0:8000 ...
```

This uses Python's built-in `wsgiref.simple_server`, which is single-threaded
and intended for local iteration only.
s
### API (production)

```bash
waitress-serve --port=8000 --call backend.app:create_app
```

`create_app()` is a factory, hence `--call`. See [§15](#15-deployment).

### Collector

```bash
python -m backend.collector.collector
```

Logs at INFO to stdout. It polls every active container every
`COLLECTOR_INTERVAL_SECONDS`, and runs a retention pass on an in-memory hourly
tick inside the same loop.

The collector never exits on error. A failure is caught **per container**, not
per loop iteration, so one unreachable container — or a fully down LXD daemon —
produces warnings and the loop continues. That is the brief's independence
requirement made concrete.

### Smoke check

```bash
curl -s localhost:8000/health
# {"status": "ok", "lxd_reachable": true}
# or {"status": "degraded", "lxd_reachable": false}
```

---

## 7. API reference

All responses are JSON. All error bodies use Falcon's shape:

```json
{ "title": "Quota exceeded", "description": "Would exceed RAM quota by 512MB (…)" }
```

The `description` is written to be shown to the user verbatim — it names the
specific rule that was broken, the exact excess, or the precise LXD error. The
frontend passes it straight through rather than substituting generic copy.

**Role column:** _public_ = no session needed · _auth_ = any signed-in user ·
_access_ = signed-in **and** assigned to that container (admins bypass) ·
_admin_ = admin role only.

| Method | Path                                   | Role   | Description                                                                                                     |
| ------ | -------------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------- |
| GET    | `/health`                              | public | `{status, lxd_reachable}`. Always 200, even when LXD is down                                                    |
| GET    | `/api/auth/google/login`               | public | 302 → Google consent screen                                                                                     |
| GET    | `/api/auth/google/callback`            | public | Exchanges the code, sets cookies, 302 → `FRONTEND_ORIGIN`                                                       |
| GET    | `/api/auth/me`                         | auth   | `{id, email, role, status}` or 401                                                                              |
| POST   | `/api/auth/logout`                     | public | Deletes the server-side session row, clears both cookies                                                        |
| GET    | `/api/containers`                      | auth   | Admin: all active containers. User: only assigned ones. Each row enriched with live `lxd_status` / `lxd_config` |
| POST   | `/api/containers`                      | admin  | Create a container in LXD and record it. 201                                                                    |
| GET    | `/api/containers/{id}`                 | access | Container record enriched with live LXD state / details. **403, not 404**, when a non-admin is not assigned     |
| PATCH  | `/api/containers/{id}`                 | admin  | `{"action": …}` **xor** `{"limits": {…}}`                                                                       |
| DELETE | `/api/containers/{id}`                 | admin  | Delete from LXD, then soft-delete the row                                                                       |
| GET    | `/api/lxd/options`                     | admin  | Host images, networks, storage pools. Degrades with `stale`                                                     |
| GET    | `/api/users`                           | admin  | All users, each enriched with current `allocation`                                                              |
| POST   | `/api/users`                           | admin  | Invite a user (`status='invited'`). 201                                                                         |
| PATCH  | `/api/users/{id}`                      | admin  | Update role, status, and/or quotas                                                                              |
| POST   | `/api/users/{uid}/containers/{cid}`    | admin  | Grant access. Quota-checked. 201                                                                                |
| DELETE | `/api/users/{uid}/containers/{cid}`    | admin  | Revoke access (soft, `active=0`)                                                                                |
| GET    | `/api/metrics/latest?container={id}`   | access | Newest raw sample, or `{"point": null}`                                                                         |
| GET    | `/api/containers/{id}/history?window=` | access | Pre-downsampled series for `1h`\|`24h`\|`7d`                                                                    |
| GET    | `/api/metrics/recent?container={id}`   | access | Last N raw samples (default 60, up to 720) for immediate graph pre-seeding                                      |
| POST   | `/api/containers/{id}/exec`            | access | Run one command; returns `{exit_code, stdout, stderr}`                                                          |
| GET    | `/api/accounting`                      | admin  | Host capacity, total allocation, per-user allocation vs quota                                                   |

### Selected request bodies

**POST `/api/containers`**

```json
{
	"name": "web-server-01",
	"image": "ubuntu:22.04",
	"limits": { "ram_mb": 1024, "cpu": 1.0, "disk_gb": 10 },
	"network": "",
	"storage_pool": "",
	"ephemeral": false,
	"autostart": false,
	"description": "",
	"assign_to": "user-uuid"
}
```

Only `name` and `image` are required. `limits` values default to `0`, which
means _unlimited_. Blank `network`/`storage_pool` inherit the default LXD
profile. `assign_to` pre-assigns the container and is quota-checked first.

Name validation is re-run server-side against
`^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$` regardless of what the form did — the
frontend's copy of the rule is a UX nicety, and an attacker can bypass it
entirely.

**PATCH `/api/containers/{id}`** — exactly one of:

```json
{ "action": "start" }   // start | stop | restart | freeze | unfreeze
{ "limits": { "ram_mb": 2048 } }   // omitted keys keep their current value
```

Supplying both, or neither, is a 400.

**POST `/api/containers/{id}/exec`**

```json
{ "command": ["echo", "hello"] }
```

A plain string is **rejected**, not joined — see [§13](#13-security-decisions).

### Status codes used

| Code | Meaning in this API                                                                         |
| ---- | ------------------------------------------------------------------------------------------- |
| 200  | Success                                                                                     |
| 201  | Created (container, user invite, assignment)                                                |
| 400  | Malformed body, invalid field, or quota exceeded                                            |
| 401  | No session, or expired/tampered token                                                       |
| 403  | Authenticated but not permitted — including "container you are not assigned to"             |
| 404  | Genuinely absent, and only where disclosure is safe (users, admin-scoped container lookups) |
| 409  | Duplicate — container name or user email already exists, or already assigned                |
| 410  | The container has been soft-deleted                                                         |
| 503  | LXD unreachable, **or** SQLite write-lock contention. Retryable                             |

---

## 8. Data model

### SQLite (`DATABASE_PATH`)

Five tables, created by `db/schema.sql`. Every query in the codebase lives in
`db/repo.py` and uses parameterized placeholders — never string interpolation.

**`users`** — one row per invited or signed-in account.

| Column          | Type        | Notes                                     |
| --------------- | ----------- | ----------------------------------------- |
| `id`            | TEXT PK     | UUID                                      |
| `email`         | TEXT UNIQUE | Lowercased before storage                 |
| `role`          | TEXT        | `CHECK IN ('admin','user')`               |
| `status`        | TEXT        | `CHECK IN ('invited','active','revoked')` |
| `quota_ram_mb`  | INTEGER     | Default 0 = unlimited                     |
| `quota_cpu`     | REAL        | Default 0 = unlimited                     |
| `quota_disk_gb` | INTEGER     | Default 0 = unlimited                     |
| `created_at`    | TEXT        | ISO-8601 UTC                              |

Rows are never hard-deleted; revoked users keep their row so audit history
stays referentially intact.

**`sessions`** — one row per live refresh token. Stores
`refresh_token_hash` (SHA-256) and never the raw value, so a leaked database
dump does not hand out working sessions. Hard-deleted on logout, because a
revoked session has no audit value once the `audit_log` records the event.

**`containers`** — our internal record of every managed container.

| Column                                       | Notes                                                                                           |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `id`                                         | Our UUID. **Stable across LXD renames** — assignments, metrics, and audit rows all key off this |
| `lxd_name`                                   | UNIQUE. The _current_ LXD-side name; looked up fresh at call time                               |
| `image`, `created_by`, `created_at`          |                                                                                                 |
| `limit_ram_mb`, `limit_cpu`, `limit_disk_gb` | DB-cached copies of the LXD limits                                                              |
| `deleted_at`                                 | Soft delete. NULL = active                                                                      |

Limits are cached here so quota arithmetic is fast and works even when LXD is
slow or down. The cache is written on create and on every successful limit
update.

Soft delete rather than `DELETE` because the row must outlive the container:
audit entries reference its id, TinyFlux history is keyed by it, and assignment
rows retain their meaning.

**`assignments`** — user ⇄ container access. Revoking sets `active = 0` rather
than deleting, preserving the grant/revoke history.

**`audit_log`** — insert-only. Every destructive or limit-changing action
writes here: `container.create`, `container.start|stop|restart|freeze|unfreeze`,
`container.limits`, `container.delete`, `container.exec`, `assignment.grant`,
`assignment.revoke`, `user.invite`, `user.update`, `auth.bootstrap_admin`.
`detail` is free text — for exec it records the exact argv that ran.

### TinyFlux (`TINYFLUX_PATH`)

One database, one measurement. Every point carries two tags:

| Tag            | Values                |
| -------------- | --------------------- |
| `container_id` | Our container UUID    |
| `resolution`   | `raw` \| `5m` \| `1h` |

The `resolution` tag is what lets a single store serve three granularities
without separate tables per tier — retention queries and deletes by it, and the
history endpoint filters by it.

Fields written per point (all floats):

| Field          | Meaning                                                                            |
| -------------- | ---------------------------------------------------------------------------------- |
| `cpu_usage_ns` | **Cumulative** CPU nanoseconds since container start — a counter, not a percentage |
| `ram_used_mb`  | Current memory usage                                                               |
| `ram_peak_mb`  | Peak memory usage (LXD's `usage_peak`)                                             |
| `disk_used_mb` | Root disk usage                                                                    |
| `net_rx_bytes` | Summed across all interfaces                                                       |
| `net_tx_bytes` | Summed across all interfaces                                                       |
| `pid_count`    | Process count                                                                      |

`cpu_usage_ns` being a monotonic counter matters to consumers: turning it into
a percentage requires differencing two consecutive samples. The rollup is built
around that — counters keep the last reading in each bucket rather than being
averaged, so the difference between two `5m` or `1h` points is still exactly
what the counter advanced between them. Gauges like `ram_used_mb` are averaged
and meaningful at every resolution. See **Retention** below for the full
per-field classification.

---

## 9. Authentication and authorization

### Sign-in flow

```text
browser ──▶ GET /api/auth/google/login
                ├─ generate_oauth_state()          ← random per attempt
                ├─ Set-Cookie: oauth_state (httpOnly, Lax, 10 min)
                └─▶ 302 to Google consent, ?state=<same value>
Google  ──▶ GET /api/auth/google/callback?code=…&state=…
                ├─ _verify_state(req)              ← 400 unless the echoed
                │                                    state matches the cookie
                ├─ exchange_code_for_tokens(code)
                ├─ verify_and_decode_id_token(id_token)   ← the only place an
                │                                            email is trusted
                ├─ look up / bootstrap / reject the user
                ├─ mint access JWT + opaque refresh token
                ├─ INSERT INTO sessions (hash only)
                └─▶ 302 to FRONTEND_ORIGIN with both cookies set
```

**Login CSRF.** The `state` check is the first thing the callback does, and it
runs before the code is exchanged. Without it the callback accepts an
authorization code from anyone: an attacker starts their own sign-in, stops at
the callback holding an unredeemed code, and gets a victim to load that URL —
the victim's browser ends up holding a session for the _attacker's_ account,
with nothing on screen looking wrong. An attacker can put anything in the query
string but cannot write an `httpOnly` cookie on our origin, so the two halves
cannot be made to agree. Verifying first also means a forged callback never
reaches Google, so it cannot be used to burn authorization codes or outbound
requests.

`SameSite=Lax` on that cookie is required rather than a compromise: the
callback arrives as a top-level cross-site navigation from
`accounts.google.com`, which is exactly the case Lax permits and `Strict`
withholds. A `Strict` state cookie would be absent at the callback and every
legitimate sign-in would fail verification. The cookie is cleared once
consumed, making each value single-use.
See `backend/tests/integration/test_login_csrf.py`.

**Admin bootstrap.** Only the address in `ADMIN_BOOTSTRAP_EMAIL` is
auto-created as an admin on first sign-in. Every other unknown email is
rejected with 403 _"Not invited"_. This is an explicit env var rather than a
"first sign-in wins" rule, which would be a race anyone who found the URL first
could win.

**Invited → active.** An invited user's status is upgraded on their first
successful sign-in. A `revoked` user is refused with 403 even if their cookies
are still valid, because status is re-read from the database on the callback.

### Tokens and cookies

|                    | Access token                                                 | Refresh token                       |
| ------------------ | ------------------------------------------------------------ | ----------------------------------- |
| Format             | JWT, HS256, `{user_id, role, exp, iat}`                      | Opaque, `secrets.token_urlsafe(32)` |
| Lifetime           | 30 minutes (default, set by `ACCESS_TOKEN_LIFETIME_MINUTES`) | 7 days                              |
| Cookie             | `access_token`, `max_age=1800` (default)                     | `refresh_token`, `max_age=604800`   |
| Stored server-side | No                                                           | Yes — SHA-256 hash only             |

Both cookies are `httpOnly` (JavaScript cannot read them, so XSS cannot
exfiltrate them), `SameSite=Lax`, `path=/`, and `Secure` when
`SESSION_COOKIE_SECURE=true`.

A third cookie, `oauth_state`, carries the same attributes but exists only
between the login redirect and the callback (`max_age=600`). It holds no
identity and grants no access — it is a nonce, and the callback deletes it on
use. It is `httpOnly` for the same reason as the others: a value JavaScript
could read is a value an XSS on our origin could forge a matching callback for.

Because the frontend is served from a different origin in development, browser
fetches must send `credentials: "include"`.

### Enforcement

`AuthMiddleware.process_resource` decodes the cookie and sets
`req.context.user` to `{"id", "role"}` — or `None`. **It never rejects a
request.** Public routes (health, login, callback, logout) simply work with a
`None` user; every other handler calls one of:

- **`require_role(req, "admin")`** — 401 if unauthenticated, 403 if the role
  does not match.
- **`require_container_access(req, container_id)`** — 401 if unauthenticated,
  403 if a non-admin has no active assignment.

`require_container_access` re-queries the `assignments` table **on every
call** and caches nothing. If an admin revokes access mid-session, the very
next exec or metrics poll is rejected — not served from a stale "they had
access a minute ago".

This split keeps public routes working without an exemption list in the
middleware, and the "every route requires auth" test (§14) is what ensures no
new endpoint silently skips the helper.

### CORS

```python
falcon.App(middleware=[
    falcon.CORSMiddleware(allow_origins=config.frontend_origin,
                          allow_credentials=config.frontend_origin),
    AuthMiddleware(),
])
```

Two details in that snippet are load-bearing:

1. **`CORSMiddleware` is listed first.** Falcon unwinds _response_ middleware
   in reverse order, so being first means its `process_response` runs last and
   the CORS headers land on _every_ reply — including error replies raised from
   deeper middleware or resource code. Without that, a 401 reaches the browser
   stripped of CORS headers, the browser refuses to expose the response, and
   the frontend cannot tell "not signed in" (redirect to login) from "server
   unreachable" (show an error).
2. **The origin is pinned, never `*`.** A wildcard is illegal in a credentialed
   CORS response and browsers reject it outright; echoing only the configured
   origin also stops arbitrary sites making cookie-bearing calls on a signed-in
   user's behalf.

---

## 10. Quotas

Each user carries three quotas: `quota_ram_mb`, `quota_cpu`, `quota_disk_gb`.

**`0` means unlimited.** The check is skipped entirely for a resource whose
quota is 0, which is why bootstrap admins are created with all three at 0.

`lxd/quota.py` exposes two functions:

- **`compute_user_allocation(user_id, conn=None)`** — sums the DB-cached limits
  across every container actively assigned to that user, skipping soft-deleted
  ones. Returns `{ram_mb, cpu, disk_gb}`.
- **`check_quota(user_id, additional_ram_mb, additional_cpu, additional_disk_gb, conn=None)`**
  — returns `(True, "")` or `(False, reason)`. The reason names the exact
  resource and the exact excess, e.g. _"Would exceed RAM quota by 512MB
  (current: 1536MB + requested: 1024MB = 2560MB, quota: 2048MB)"_, so the admin
  can see precisely what to adjust.

### Where quota is enforced

| Path                                              | Checked against                                |
| ------------------------------------------------- | ---------------------------------------------- |
| `POST /api/containers` with `assign_to`           | The named assignee                             |
| `POST /api/users/{uid}/containers/{cid}` (grant)  | The grantee                                    |
| `PATCH /api/containers/{id}` with larger `limits` | **Every** active assignee, using the **delta** |

The PATCH rule is the subtle one. The check uses `new - old`, not the absolute
value, because a quota bounds a user's _total_ allocation. And it iterates
`repo.list_assignees()` rather than checking `created_by`: the creator is the
admin who made the container, and admins are typically unlimited, so checking
them meant the check never fired — grant a small container, then grow it, and
the assignee's quota was bypassed entirely.

### Concurrency

A quota check that authorises a write must see the same snapshot the write
lands in. `repo.transaction()` wraps `BEGIN IMMEDIATE`, taking SQLite's
RESERVED lock up front:

```python
with repo.transaction() as conn:
    container = repo.get_container_by_id(container_id, conn=conn)
    ...
    allowed, reason = check_quota(user_id, …, conn=conn)
    if not allowed: raise falcon.HTTPBadRequest(...)
    repo.assign_container(user_id, container_id, conn=conn)
```

Without it, two concurrent grants both read the same pre-grant allocation, both
conclude they fit, and both insert — a quota that bounds each request
individually bounds nothing under load. Repository functions that can join a
caller's transaction take an optional `conn=`.

If the lock cannot be acquired within the 5-second busy timeout,
`repo.WriteLockTimeout` is raised and a **central error handler in `app.py`
maps it to 503**, not 500 — contention is capacity, not a bug, and 503 tells
the client to retry.

---

## 11. Metrics pipeline and retention

```text
LXD ──10s──▶ collector ──▶ TinyFlux(raw) ──▶ /api/metrics/latest
                              │                /api/containers/{id}/history
                        hourly retention
                              ▼
                    raw >24h  →  5m buckets
                    5m  >7d   →  1h buckets
                    any >90d  →  deleted
```

### Collection

`collector.py` reads `repo.list_active_containers()` each cycle and calls
`lxd_client.get_container_state()` per container, writing one `raw` point each.
Failures are caught per container — `LXDUnavailableError` for LXD problems, a
broad `Exception` for anything else (TinyFlux I/O, DB trouble) — logged at
warning level, and the loop continues.

Sleep happens **between iterations**, not between containers, so all containers
in one cycle share an approximate timestamp window.

### Retention

`retention.py` runs one pass per hour, triggered by an in-memory timestamp
inside the collector loop. It is not a second process and it does not need to
survive a restart: the worst case after a restart is running slightly early,
which is harmless.

Per container, three steps:

1. `raw` points older than **24h** → grouped into 5-minute buckets, reduced,
   written as `5m`, originals deleted.
2. `5m` points older than **7d** → grouped into 1-hour buckets, reduced,
   written as `1h`, originals deleted.
3. Anything older than **90d** → deleted outright, any resolution.

A rolled-up point keeps the same field names as a `raw` one, so a `5m` point has
the same shape. Each container is wrapped in its own try/except so one failure
never stops the rest.

#### How a bucket is reduced

Not every field can be averaged. `_reduce_fields` dispatches on what the field
means:

| Kind                              | Fields                                         | Reduction            |
| --------------------------------- | ---------------------------------------------- | -------------------- |
| Gauge — instantaneous reading     | `ram_used_mb`, `disk_used_mb`, `pid_count`     | mean                 |
| Counter — monotonic running total | `cpu_usage_ns`, `net_rx_bytes`, `net_tx_bytes` | last value in bucket |
| High-water mark                   | `ram_peak_mb`                                  | max                  |

The counters are read as **rates** by every consumer — the frontend subtracts
consecutive points and divides by elapsed time. Keeping each bucket's final
reading makes that subtraction exact regardless of how the traffic was shaped
inside the bucket, and the stored value is still a total the counter really
reached. Averaging a counter yields a number it held only in passing, and
reconstructs the right rate _only_ if the counter advanced uniformly across the
bucket — so a burst gets reported at part of its true height with the remainder
smeared into the next bucket. `ram_peak_mb` takes the max because a mean of
peaks is lower than every peak it summarises.

Last value rather than `max` matters only on a **counter reset** (a container
restart zeroes LXD's counters): last value keeps the drop visible so the
frontend recognises it and rebaselines, whereas `max` would report the pre-reset
high and invent traffic in a bucket that saw none.

Two supporting details. Points are sorted by timestamp before reduction, because
retention writes back-dated points and the CSV is not chronological — "last
reading" has to mean latest time, not last element. And a field missing from a
point is skipped rather than counted as `0.0`, so one missed collection does not
halve a gauge or look like a counter reset.

Adding a metric field to `get_container_state()` without classifying it fails
`test_every_collected_field_is_classified`, which checks the classification
against the real function rather than a copied list.

### Serving

Both metrics endpoints read **TinyFlux only and never call LXD**. Two
consequences that motivated the rule:

- Dashboard traffic — however many tabs are open, polling every 5 seconds —
  adds zero load to the LXD daemon. Only the collector polls LXD, on its fixed
  timer.
- The endpoints keep serving last-known data while LXD is slow or down.

History windows are served from the pre-downsampled tier, never by aggregating
raw points on the fly:

| `window`       | Span     | Resolution read |
| -------------- | -------- | --------------- |
| `1h` (default) | 1 hour   | `raw`           |
| `24h`          | 24 hours | `5m`            |
| `7d`           | 7 days   | `1h`            |

So a 24-hour chart transfers ~288 points, not the ~17,280 raw samples that
window contains. An unrecognised `window` is a 400.

`GET /api/metrics/latest` returns `{"container_id": …, "point": null}` rather
than 404 when a container has no samples yet — a container created seconds ago
is a normal state, not an error, and the tile renders "no data yet".

---

## 12. Error handling conventions

**LXD failures are 503, never 500.** Every call into `lxd/client.py` from a
resource is wrapped, and an unreachable daemon produces
`HTTPServiceUnavailable` with the underlying LXD message in `description`. A
500 says "this server is broken"; 503 says "this dependency is down, retry" —
and the frontend renders a retry prompt instead of a crash page.

**Write-lock contention is 503.** Registered once in `app.py` via
`app.add_error_handler(repo.WriteLockTimeout, …)` so no transactional endpoint
can forget it.

**Read endpoints degrade rather than fail.** `/api/accounting` and
`/api/lxd/options` both catch `LXDUnavailableError` and return their
DB-derived (or empty) portions with `stale: true` and an explanatory
`*_error` string. An admin sees a banner over a working table rather than an
error page with nothing on it. `stale` exists precisely so the client can tell
"this host genuinely has no custom networks" from "we could not ask" —
presenting the second as the first would show an empty dropdown that looks
authoritative.

**`/health` never fails.** It always returns 200; the _body_ carries
`status: "ok"` or `"degraded"`. `check_lxd_reachable()` is written never to
raise, because a crash there would hide the real issue (LXD down) behind a 500.

**Malformed input is 400, not 500.** Falcon hands `req.get_media()` back as
whatever the client's JSON decoded to, with no type checking. A handler calling
`body.get("name").strip()` on a JSON `null` raises `AttributeError` → 500,
telling an honest client the server is broken when its request was simply
malformed. `resources/validation.py` converts that class of mistake into the
400 it always was:

| Helper                         | Guards                                                                  |
| ------------------------------ | ----------------------------------------------------------------------- |
| `require_object`               | Top-level body is a JSON object                                         |
| `validate_string`              | Value is a string (rejects `bool` explicitly)                           |
| `validate_non_negative_number` | Numeric and ≥ 0 (rejects `bool`, since `isinstance(True, int)` is True) |
| `validate_bool`                | A real JSON boolean — `"true"` and `1` are rejected, not coerced        |
| `validate_limits`              | The `limits` sub-object, with per-path defaults                         |

The negative-number check closed a real hole: a negative limit used to pass the
cast and then get silently dropped by an `if ram_mb > 0` guard, so the container
was created with _no_ limit while the response echoed the negative number back
as though it had been applied.

`validate_bool` refuses to coerce for a concrete reason: guessing wrong on
`ephemeral` destroys the container on its first stop.

**Duplicate keys are 409.** `repo.DuplicateKeyError` wraps
`sqlite3.IntegrityError` so callers map "already exists" to 409 without
importing `sqlite3` or matching driver strings. A pre-check cannot close the
race — two concurrent invites for one address both pass it, and only the UNIQUE
constraint separates them — so the loser gets 409, not a 500.

---

## 13. Security decisions

**403, not 404, for unassigned containers.** A non-admin requesting a container
they are not assigned to gets 403 — and so does a non-admin requesting an id
that does not exist at all. Returning 404 for the second case would let an
attacker enumerate valid container IDs by probing for the difference. This is
an explicit requirement from the brief and is asserted in
`tests/integration/test_security.py`.

**Authorization is server-side only.** The container list is scoped by the
`assignments` table inside the handler, not filtered in the browser. A regular
user's `GET /api/containers` never contains a container they cannot access, so
there is nothing for a modified client to reveal.

**Exec takes an argv array, never a shell string.**

```text
{ "command": ["echo", "hello"] }      ✅  accepted
{ "command": "echo hi; rm -rf /" }    ❌  400
```

The array is handed to pylxd's exec as an argument list, so there is no shell
to inject into. Rejecting a plain string is the actual security boundary, not
tidiness: that second body, if it ever reached a shell, runs two commands; as a
rejected non-array it runs none. Each element must itself be a string, so a
nested list or object cannot smuggle structure past the check. The container's
_current_ `lxd_name` is looked up at call time, and the exact argv is written to
the audit log.

**No raw secrets at rest.** Refresh tokens are stored as SHA-256 hashes.
`JWT_SECRET` lives only in the environment. `.env` is untracked.

**SQL is always parameterized.** Every statement in `repo.py` uses `?`
placeholders. The one dynamic fragment — `update_user`'s SET clause — builds
column names from a hardcoded whitelist (`_UPDATABLE_USER_FIELDS`) and raises
`ValueError` on anything else, so `id` and `email` cannot be written after
creation.

**Foreign keys are enabled per connection.** SQLite defaults `PRAGMA
foreign_keys` to OFF, so `get_connection()` turns it on every time; without
that, `REFERENCES` constraints are silently ignored.

**The last admin cannot be locked out.** `PATCH /api/users/{id}` refuses any
change that would leave zero users with `role='admin' AND status='active'`. The
guard checks _both_ fields, because revoking the last admin locks the system
just as effectively as demoting them.

**ID tokens are verified, not decoded.** `verify_and_decode_id_token()` is the
only place an email is trusted as belonging to a real person; it checks
Google's signature rather than reading unverified claims.

---

## 14. Testing

```bash
# Everything
python -m pytest backend/tests/ -q

# Unit only / integration only
python -m pytest backend/tests/ -q --ignore=backend/tests/integration
python -m pytest backend/tests/integration/ -q
```

**Success criteria:** zero failures. One `xfail` is expected and correct (see
below); the four skips are by design.

### Layout

`tests/conftest.py` creates a temp SQLite file and sets `DATABASE_PATH` **at
module import time**, before pytest collects any test module that imports
backend code — necessary because `config.py` validates the environment at
import.

| Suite                                                                                               | Level       | Establishes                                                                                                                                                                                                   |
| --------------------------------------------------------------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_db.py`                                                                                        | unit        | Repository CRUD, soft-delete, duplicate handling                                                                                                                                                              |
| `test_tsdb.py`                                                                                      | unit        | Point write/query/remove, tag filtering                                                                                                                                                                       |
| `test_quota.py`                                                                                     | unit        | Allocation sums, per-resource messages, 0 = unlimited                                                                                                                                                         |
| `test_retention.py`                                                                                 | unit        | Three-tier rollup with an injected clock                                                                                                                                                                      |
| `test_users.py`, `test_assignments.py`, `test_metrics.py`, `test_terminal.py`, `test_accounting.py` | unit        | Per-resource behaviour                                                                                                                                                                                        |
| `test_auth_required.py`                                                                             | unit        | Hand-maintained protected-route list; OAuth URL construction and `state` generation properties                                                                                                                |
| `integration/test_route_coverage.py`                                                                | integration | **Walks Falcon's router** — every registered route is discovered, not declared, and must 401 unless it is in the four-entry `PUBLIC` set. JWT tampering, `alg=none`, expired and cross-signed tokens rejected |
| `integration/test_lifecycle.py`                                                                     | integration | One ordered story: invite → grant → create → start → metrics → limit change → exec → revoke → delete → audit                                                                                                  |
| `integration/test_login_csrf.py`                                                                    | integration | The OAuth `state` binding: forged, missing, empty, mismatched and replayed callbacks are refused **before** Google is contacted; a genuine round-trip still signs in                                          |
| `integration/test_refresh.py`                                                                       | integration | Refresh-token redemption and the four ways it must refuse (absent, forged, expired, revoked account)                                                                                                          |
| `integration/test_security.py`                                                                      | integration | Tenant isolation, privilege boundaries, SQL and command injection, audit integrity                                                                                                                            |
| `integration/test_resilience.py`                                                                    | integration | Per-endpoint behaviour during a simulated LXD outage; malformed input                                                                                                                                         |
| `integration/test_pipeline.py`                                                                      | integration | LXD → collector → TinyFlux → retention → HTTP, with an injected clock                                                                                                                                         |
| `integration/test_integrity.py`                                                                     | integration | Schema constraints, soft-delete invariants, audit immutability, concurrency                                                                                                                                   |

### Two levels, two stub depths

Unit tests monkeypatch the **wrapper functions** in `backend.lxd.client`, which
means the wrapper's own logic — the bytes-to-MB conversion, the try/except that
turns LXD errors into `None`, the sockets→cores→threads walk — never runs.

Integration tests replace **`_get_client`**, one level deeper, against the
in-memory double in `integration/fake_lxd.py`. Every real line of `client.py`
executes. The double models exactly what `client.py` touches and nothing else,
so a future call to an unmodelled attribute fails loudly instead of silently
passing. `set_down()` simulates an unreachable daemon by making the client
factory itself raise — which is what actually happens when the socket is
missing.

### The route-coverage inversion

`test_route_coverage.py` keeps a list of _public_ routes rather than protected
ones. Forgetting to update it makes a new route **stricter** in the test's eyes,
not laxer — so the failure mode is a visible red test, not a silent hole. The
four public entries are `GET /health`, `GET /api/auth/google/login`,
`GET /api/auth/google/callback`, and `POST /api/auth/logout`; each is
positively tested by `test_public_routes_are_reachable_without_auth`, so both
directions are covered.

### The expected xfail

`TestTinyFluxIndexBehaviour::test_remove_after_an_out_of_order_insert_corrupts_the_time_index`
characterises the TinyFlux 1.2.0 defect described in [§2](#2-technology-choices)
using raw library calls. We worked around the bug rather than patching the
library, so this characterisation **must keep failing**. It XPASSes only when a
TinyFlux upgrade fixes the root cause — which is exactly the signal the version
pin exists to catch.

### Recorded results

The backend test suite executes with **378 passed, 4 skipped, 1 xfailed, 0 failed** across 383 collected items in ~3.9s. All seven integration defects identified during development remain resolved, and the one expected TinyFlux 1.2.0 library characterisation xfail remains documented and pinned. The post-mortems for those seven defects are in this section rather than a separate report, so they stay in the repository alongside the code they describe.

---

## 15. Deployment

The production topology is two systemd units on the LXD host, plus a static
frontend build served by any web server.

- **API:** `waitress-serve --port=8000 --call backend.app:create_app`
- **Collector:** `python -m backend.collector.collector`

Both need the working directory set to the repository root, the virtualenv's
Python, and an `EnvironmentFile=` pointing at the production environment — real
env vars take precedence over `backend/.env`, so a stale dev file on the box
cannot override production values.

Production checklist:

- [ ] `SESSION_COOKIE_SECURE=true` (requires HTTPS end to end)
- [ ] `GOOGLE_REDIRECT_URI` uses the public HTTPS hostname and is registered
      verbatim in the Google Cloud Console
- [ ] `FRONTEND_ORIGIN` is the exact public origin — it is both the CORS
      allowlist and the post-login redirect target
- [ ] `JWT_SECRET` is freshly generated, not the value from `.env.example`
- [ ] `DATABASE_PATH` and `TINYFLUX_PATH` are on persistent storage, writable
      by the service account, and included in backups
- [ ] The service account can reach the LXD socket
- [ ] `python -m backend.db.init_db` has been run once

The production deployment files are provided in the repository:

- **API Service Unit:** [`deploy/hsm-api.service`](../deploy/hsm-api.service)
- **Collector Service Unit:** [`deploy/hsm-collector.service`](../deploy/hsm-collector.service)
- **Deployment Guide:** [`deploy/notes.md`](../deploy/notes.md) (step-by-step service account creation, file permissions, systemctl enable/start)
- **Resource Footprint Script:** [`scripts/measure_footprint.sh`](../scripts/measure_footprint.sh) (idle RSS/CPU profiling)

---

## 16. Known limitations

**`limit_disk_gb` is an allocation ledger, not an enforced quota.** The column
is validated, counted against `quota_disk_gb` when a container is created or
granted, and reported by `/api/accounting` — so it correctly prevents an admin
from _over-committing_ disk on paper. What it does not do is bound the root
filesystem: `create_container` builds `lxd_limits` from `limits.memory` and
`limits.cpu` only, and selecting a storage pool sets `devices.root.pool`
without a `size` key. A container can therefore fill the host disk while
sitting inside its nominal 10GB allocation.

Adding the `size` key is not the fix it appears to be, because whether LXD
honours it depends on the pool's storage driver. Measured on this host, whose
only pool uses the `dir` driver:

```console
$ lxc config device override hsm-probe root size=2GB
Device root overridden for hsm-probe
$ lxc config device show hsm-probe
root:
  path: /
  pool: default
  size: 2GB      # <-- recorded
  type: disk
$ lxc exec hsm-probe -- df -h /
Filesystem      Size  Used Avail Use% Mounted on
/dev/sdd       1007G   14G  943G   2% /   # <-- not enforced
```

`dir` accepts the key silently and ignores it — no error, no warning. Setting
it unconditionally would be worse than the present gap: `lxc config` and the
dashboard would both display a 2GB quota that does not exist, turning a visible
limitation into a false guarantee. Real enforcement needs a size-capable driver
(`btrfs`, `zfs`, `lvm`, or `ceph` — which support quotas via subvolume limits,
refquota, or block-device sizing), so the honest implementation reads
`driver` from `list_storage_pools()`, applies `size` only on a pool that can
honour it, and refuses the request otherwise rather than pretending. That is a
behaviour change requiring a driver-capability matrix and a migration path for
containers already created without a size, so it is documented here rather than
half-implemented.

**`GET /api/lxd/options` lists only locally cached images.** Remote aliases
like `ubuntu:22.04` resolve through _remotes_, a client-side CLI concept the
LXD HTTP API does not expose. A fresh host with an empty image cache returns an
empty list even though thousands of images are installable — which is why the
creation form keeps its image field free-text and treats this list as
suggestions rather than the valid set.

**`GET /api/containers/{id}` degrades gracefully when LXD is down.** When LXD is available, the endpoint merges the database record with live LXD details (`lxd_status`, `lxd_architecture`, `lxd_ip_addresses`, `lxd_image`, `lxd_process_count`). If LXD is unreachable, it falls back to the database record with `lxd_status: "Unknown"` without throwing a 500 error.

**Refresh tokens are not rotated on use.** `POST /api/auth/refresh` redeems the
refresh cookie for a new access token, re-reading role and status from the
database so a demotion or revocation applies at the next refresh rather than
persisting inside an already-signed token. The refresh token itself is
deliberately reused rather than rotated: rotation makes a stolen token
single-use, but without replay detection two dashboard tabs refreshing at once
each present the same valid token and the loser of the race is logged out by
its own sibling. Doing it safely needs a used-token grace window plus
family-wide revocation, which is more machinery than this project warrants. The
token stays server-side revocable through the `sessions` row, and that
revocability is the property being bought.

**SQLite write serialization is a single-host ceiling.** `BEGIN IMMEDIATE`
serializes quota-critical writes with a 5-second busy timeout. That is correct
and sufficient for one host, and callers that wait it out receive a retryable
503 — but it is not a path to horizontal scale.

**The collector's retention tick is in-memory.** Restarting the collector
resets the hourly timer, so a pass may run earlier than an hour after the last
one. Harmless — the operations are idempotent with respect to what has already
been rolled up.
