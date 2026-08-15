# Final Report — Hobby Server Monitor

---

## Key Decisions

### 7.1 — Session Persistence and Logout

**Chose:** Short-lived JWT access token (30 min) + long-lived opaque refresh token (7 days), both stored as `httpOnly`, `Secure`, `SameSite=Lax` cookies. The refresh token's SHA-256 hash is stored in the `sessions` SQLite table. `POST /api/auth/refresh` redeems it for a new access token; the frontend's `apiFetch` wrapper retries any 401 once behind a refresh, so expiry is invisible to the user. Logout deletes the row server-side, which is what makes the refresh token unredeemable rather than merely absent from the browser. A third, short-lived `oauth_state` cookie binds each sign-in attempt to the browser that began it.

Two properties are enforced at redemption rather than trusted from the token. Expiry is checked in Python, because `expires_at` is ISO text and no SQL predicate filters on it — without that check the row would outlive its own expiry and keep minting access tokens, making the 7-day lifetime decorative. The user's role and status are re-read from the database, so a demotion or revocation takes effect at the next refresh instead of persisting for up to 7 days inside a token that was already signed.

**Not rotating the refresh token on use** is a deliberate trade-off, not an oversight. Rotation is stronger — a stolen token becomes single-use — but it needs replay detection to be safe: two dashboard tabs refreshing at once each present the same valid token, and whichever loses the race gets logged out by its own sibling. Doing that correctly means a used-token grace window plus family-wide revocation, which is more machinery than this project warrants. The token remains server-side revocable through the sessions row, and that revocability is the property being bought.

**Sign-in itself is bound to the browser that started it** by a third cookie in the same family. `GET /api/auth/google/login` mints a 32-byte CSPRNG `state`, sends it to Google in the authorization URL, and stores the same value in an `oauth_state` cookie (`httpOnly`, `Secure` when configured, `SameSite=Lax`, 10-minute lifetime). The callback compares the value Google echoed back against the cookie with `secrets.compare_digest` and refuses with 400 unless both are present and equal.

Without that comparison the callback accepts an authorization code from anyone, and the resulting attack runs the opposite direction from session theft: an attacker begins a sign-in as themselves, stops at the callback holding an unredeemed code, and gets the victim to load that URL. Our server would exchange the attacker's code and hand _the victim's browser_ a session belonging to the attacker's account — after which every container the victim creates and every command they run lands somewhere the attacker can read, with nothing on screen looking wrong. An attacker controls the query string but cannot write an `httpOnly` cookie on our origin, so the two halves cannot be made to agree.

Two details are load-bearing. The check runs **before** the code exchange, so a forged callback costs no outbound request to Google and burns no authorization code — a check that refuses only after the round trip is a rate-limiting problem wearing a security fix's clothes. And the cookie is `SameSite=Lax` rather than `Strict` _because_ the callback arrives as a top-level cross-site navigation from `accounts.google.com`: Lax is sent on exactly that, while Strict would withhold the cookie and make every legitimate sign-in fail verification. The cookie is cleared once consumed, so a completed callback cannot be replayed; it is deliberately left in place on failure, since a rejected attempt never revealed the value and clearing it would let a forged callback cancel a legitimate sign-in still in flight. `backend/tests/integration/test_login_csrf.py` asserts all of this, including that a refused callback never reaches Google.

**Alternatives considered:**

- `localStorage`-based token storage.
- Server-side session state only (lookup on every HTTP request).
- Rotating refresh tokens with replay detection.
- Storing the OAuth `state` in a server-side table keyed by a session id, or signing it into a JWT, instead of a plain cookie.

**Why rejected:** `localStorage` is vulnerable to token theft via Cross-Site Scripting (XSS). Server-side sessions alone require a database lookup on every single request, increasing query overhead and latency. The `httpOnly` cookie approach completely isolates token storage from client JavaScript, while the short-lived JWT allows fast, stateless local verification in `AuthMiddleware`. Server-side refresh token hashes ensure instant, definitive session revocation upon logout. Rotation was rejected for the concurrency reason given above: without replay detection it makes multi-tab use unreliable, and with it the complexity exceeds what this project needs. Persisting `state` server-side was rejected because it buys nothing here: the property required is only that the two halves of one flow agree, and the cookie already supplies that without a table to write, index, and expire — a signed JWT would add a verification step to prove authorship of a value we generated seconds earlier and never let out of the browser.

---

### 7.2 — Where Authorization Decisions Get Made

**Chose:** One central `AuthMiddleware` Falcon middleware decodes and verifies the JWT on every request and attaches `req.context.user`. Explicit authorization helpers (`require_role("admin")`, `require_container_access(user, container_id)`) are invoked at the top of every protected resource handler. An automated integration test suite iterates every registered route in Falcon's routing table and asserts that unauthenticated calls return 401.

**Alternatives considered:**

- Handler decorators per route (`@require_admin`).
- Convention-based authorization.

**Why rejected:** Decorators require manual opt-in per method and are easily forgotten under rapid development. Convention-only provides no machine-verifiable guarantees. The dynamic integration test (`test_route_coverage.py`) inspects Falcon's runtime router, ensuring any newly added endpoint without auth enforcement immediately breaks the build.

---

### 7.3 — What a Quota Measures, and What Happens at the Limit

**Chose:** A quota is three numbers on the user row — `quota_ram_mb`, `quota_cpu`, `quota_disk_gb` — and the amount consumed is the sum of `limit_ram_mb`, `limit_cpu`, `limit_disk_gb` across the containers actively assigned to that user. It measures **allocation, not usage**: a user assigned two 1GB containers has 2GB allocated whether those containers are idle or thrashing. Allocation is what an admin grants and can therefore be checked before the fact; usage is a runtime observation, and refusing a grant because a container happened to be busy would make the same request succeed or fail depending on the minute.

Limits are read from the DB-cached columns rather than from live LXD calls, so a quota check stays fast and keeps working while LXD is slow or unreachable. `0` means **unlimited** — every comparison is guarded by `and user["quota_x"] > 0`, which is how admins are modelled as unbounded without a separate code path.

Hitting the limit is a **hard block: HTTP 400 naming the exact overage** (`"Would exceed RAM quota by 512MB (current: 1536MB + requested: 1024MB = 2560MB, quota: 2048MB)"`). Each resource is checked separately so the message names the one that failed rather than reporting a generic rejection.

The check fires at **three** moments, not the two originally planned:

| Site                                                        | Trigger                                | Whose quota            |
| ----------------------------------------------------------- | -------------------------------------- | ---------------------- |
| [`containers.py:251`](backend/resources/containers.py#L251) | create with `assign_to`                | the assignee           |
| [`assignments.py:98`](backend/resources/assignments.py#L98) | admin grants access                    | the grantee            |
| [`containers.py:468`](backend/resources/containers.py#L468) | limits raised on an assigned container | every current assignee |

The third was added after the first two proved insufficient: without it an admin could grant a small container and then grow it, bypassing the assignee's quota entirely. That path checks each assignee against the _delta_ only, and only when a delta is positive, so shrinking a container never fails on quota.

**Alternatives considered:**

- Measuring live usage (actual RSS / CPU seconds) instead of allocation.
- Soft limits — warn and log, but allow the operation.
- Enforcing only at assignment time.

**Why rejected:** Live usage makes the outcome of an identical request depend on transient load, which is neither reproducible nor explainable to the admin who hit it. A soft warning is easy to miss in a dashboard and leaves the system knowingly over-committed, which defeats the point of having a quota. Assignment-time-only enforcement is the hole the limit-change check closes. One caveat worth stating: only the grant path holds a `BEGIN IMMEDIATE` transaction across the read-then-write (defect B1), so the create and limit-change paths remain theoretically racy under simultaneous admin requests — acceptable on a single-admin hobby host, and the fix is the same `repo.transaction()` wrapper.

---

### 7.4 — What `pylxd`'s Privileged Access Grants, and What We Did About It

**Chose:** Membership in the host's `lxd` group is **effectively root-equivalent** — LXD can mount arbitrary host paths into a container, so anything that can talk to the socket can read and write the host filesystem as root. Nothing about this is fixed by careful application code, so the response is containment rather than mitigation, at three layers.

**One process boundary.** `pylxd.Client()` is constructed in exactly one place, [`backend/lxd/client.py:41`](backend/lxd/client.py#L41). No other module in the backend opens a connection to the daemon. (`containers.py` imports `pylxd.exceptions.LXDAPIException` — an exception class for `except` clauses, carrying no capability.) The privileged surface is one auditable file, and that is a property a reviewer can check with one `grep` rather than trust.

**One low-privilege identity.** Both units run `User=hsm-runner`, a `--system --no-create-home` account added to `lxd` and no other group — not `sudo`, not `docker`, no shell login.

**A confined runtime.** Because that group membership is itself close to root, the account is not merely unprivileged but sandboxed by systemd: `NoNewPrivileges=yes` blocks setuid escalation, `PrivateTmp=yes` isolates `/tmp`, `ProtectHome=yes` empties `/home` and `/root`, and `ProtectSystem=strict` mounts the entire filesystem read-only. `systemd-analyze security` scores the units at **8.3** with these applied, against **9.0 UNSAFE** without them.

`ProtectSystem=strict` is the load-bearing one, and it is also the one that has to be paid for: it makes _everything_ read-only, so the two paths the services genuinely write are named back explicitly via `ReadWritePaths` — the data directory, and the LXD socket's directory (connecting to an `AF_UNIX` socket is a write on its inode). Omitting the first produces `sqlite3.OperationalError: attempt to write a readonly database`, which is worth stating precisely because it looks exactly like a file-ownership problem and cannot be fixed by `chown` — the read-only mount refuses the write before permissions are consulted. `backend/tests/test_config_and_deploy.py::TestSystemdHardening` asserts every directive above and both `ReadWritePaths` entries, so a future edit that hardens the mount without restoring a writable data path fails the suite instead of failing at 3 a.m. on a deploy.

**A closed config allowlist.** The dict sent to `containers.create()` is assembled key-by-key from validated scalars — `limits.memory`, `limits.cpu`, `boot.autostart`, and nothing else. No caller-supplied dictionary is ever merged into it, and `create_container` additionally pins `security.privileged=false` last so it wins over anything in the incoming dict. This is what prevents an authenticated admin from passing `security.privileged=true`, `raw.lxc`, or a host bind-mount device through the API: those keys have no route to LXD, so containers take LXD's unprivileged defaults by construction rather than by an assertion the code would have to remember to make.

**Alternatives considered:**

- Running the API as root, or with `sudo lxc` shell-outs.
- A `polkit`/`sudoers` rule allowing a specific set of `lxc` subcommands.
- Passing a caller-supplied config dict straight to LXD for flexibility.

**Why rejected:** Root or `sudo lxc` gives away the whole host on any injection bug and adds shell quoting as a second attack surface. A `sudoers` allowlist of `lxc` subcommands sounds tighter but is porous — `lxc config set` alone can set `raw.lxc` — and it trades a typed Python API for string parsing. Forwarding a caller-supplied config dict is the single change that would undo the third layer, which is why limits are transcribed field by field even though it is more code.

**What is not done:** the systemd units carry no hardening directives — no `NoNewPrivileges`, `ProtectSystem`, `ProtectHome`, or `PrivateTmp`. On a service whose group membership is already root-equivalent these would raise the cost of an exploit without changing what the daemon socket grants, so they are a genuine improvement left undone rather than one deemed unnecessary.

---

### 7.5 — Dashboard Polling Design

**Chose:** The frontend polls `/api/metrics/latest?container=<id>` on a 5-second cadence, matching the collector's write rate. That endpoint reads the most recent point from TinyFlux TSDB — it **never** queries the LXD daemon directly. The independent background collector is the only process that ever polls LXD.

**Alternatives considered:**

- Persistent WebSocket / Server-Sent Events (SSE) push streams.
- Long-polling.
- Frontend directly invoking hypervisor APIs.

**Why rejected:** WebSockets and SSE introduce persistent connection state and connection management complexity into the WSGI server. Directly querying LXD from the frontend would create severe I/O load on the hypervisor socket as browser tabs scale. By decoupling metrics through TinyFlux, dashboard traffic scales independently of hypervisor load.

---

### 7.6 — What's in the Metric Store After a Month

**Chose:** Three retention tiers, enforced by a job the collector runs itself. TinyFlux has **no retention feature of its own** — nothing expires, nothing compacts, and the file grows without bound until a disk fills. Everything below exists because that floor had to be built.

| Age        | Resolution kept                    | Constant            |
| ---------- | ---------------------------------- | ------------------- |
| 0–24 h     | raw (one point per collector tick) | `_RAW_MAX_AGE`      |
| 24 h – 7 d | 5-minute buckets                   | `_FIVE_MIN_MAX_AGE` |
| 7 d – 90 d | 1-hour buckets                     | —                   |
| > 90 d     | deleted outright                   | `_ABSOLUTE_MAX_AGE` |

Each pass downsamples into the next tier, then prunes the source points once they have been rolled up. Points carry a `resolution: raw\|5m\|1h` tag, which is what lets one store hold all three tiers and what §7.9 selects on.

The **collector runs the pass itself, hourly**, tracked by an in-memory `_last_retention_run` timestamp against a 3600-second interval — not `cron`, not a separate unit. A hobby server should not need a second install step to avoid filling its disk, and the collector is already a supervised always-on process, so putting the job anywhere else adds a failure mode (retention silently not running) without adding capability. Because the marker is in-memory, a restart means the next pass runs up to an hour early, which is harmless — the tiers are defined by absolute age, so running early is a no-op rather than a double-rollup.

**How a bucket collapses depends on what the field means.** "Downsampled into 5-minute averages" is the brief's wording, and it is right for three of the seven fields the collector writes. It is wrong for the rest, so `_reduce_fields` dispatches on an explicit classification rather than averaging everything:

| Kind                                | Fields                                         | Reduction                |
| ----------------------------------- | ---------------------------------------------- | ------------------------ |
| Gauge — an instantaneous reading    | `ram_used_mb`, `disk_used_mb`, `pid_count`     | mean                     |
| Counter — a monotonic running total | `cpu_usage_ns`, `net_rx_bytes`, `net_tx_bytes` | last value in the bucket |
| High-water mark                     | `ram_peak_mb`                                  | max                      |

The counters are the interesting case, because averaging them is wrong in a way that testing with flat data cannot reveal. Every consumer of those three fields reads them as a **rate**, by subtracting consecutive points — `current.cpu_usage_ns - previous.cpu_usage_ns` over the elapsed time, in `ContainerResourceGraphs.tsx` and again in `MetricTile.tsx`. Keeping the bucket's final reading makes that subtraction exact: two consecutive rolled-up points differ by precisely what the counter advanced between them, whatever shape the traffic had inside the bucket, and the stored number remains a total the counter genuinely reached.

The mean fails twice. Read as a level ("bytes received so far") it is a value the counter held only in passing — low by up to a bucket's worth of traffic. Read as a rate it is correct **only if the counter advanced uniformly across the bucket**, which is exactly why the defect survived earlier testing: the mean of a uniform ramp is its midpoint, and consecutive midpoints are one bucket's growth apart, so a steady synthetic series reconstructs the right rate under either reduction. Give it a burst instead and the mean drags that bucket below its own final reading, so part of the spike is attributed to the _following_ bucket — the chart understates the peak and then draws traffic in a window that was idle. `TestCounterRateFidelity` pins both cases with numbers.

Last value rather than `max` is a deliberate choice too, and the two agree on every well-behaved bucket. They diverge on a **counter reset**: a container restart sends LXD's counters back to zero. Last value keeps the post-reset reading, so the drop stays visible, the frontend recognises the one negative delta as a reset and discards that single interval, and everything after it measures from the correct new baseline. `max` would report the pre-reset high — inventing traffic in a bucket that saw none and pushing the unavoidable gap into the following interval, where no reset happened.

Two smaller decisions in the same function. Points are sorted by timestamp before the reduction rather than trusted in store order, because retention writes back-dated rollup points and the CSV is therefore not chronological — "last reading" has to mean latest time, not last element. And a field absent from a point is skipped rather than counted as `0.0` (which is what the original code's `.get(key, 0.0)` did): one missed collection would otherwise halve a gauge, indistinguishable from real idleness, and make a counter look like it had reset.

Composing the tiers is safe for all three reductions. The last of a series of last-values is still the last value, and the max of maxima is still the max, so the 5m→1h step does not compound error. The mean of means equals the overall mean exactly when buckets hold equal sample counts — the normal case at a fixed interval, and off by a fraction of a percent when a collection was missed.

One residual imprecision, accepted: each bucket's point is timestamped at the bucket's **start**, so a counter's value is labelled up to one bucket earlier than the instant it was read. The spacing between rolled-up points is still exactly the bucket width, so every rate computed from them is correct; only the absolute placement on the x-axis shifts, by at most 5 minutes on the 24h chart and an hour on the 7d one. Timestamping at the bucket end would fix the shift and break the gauges instead, which genuinely describe the whole window.

At the shipped 5-second interval a single container produces **17,280 raw points per day**, which collapse to 288 five-minute buckets — a 60× reduction at the first tier, before the 1-hour tier reduces the surviving week by a further 12×. That ratio, not the absolute size, is what makes a server left running for months bounded.

**Alternatives considered:**

- Keep every raw point forever.
- A `cron` entry or a third systemd timer unit.
- Delete old data outright with no downsampling.
- Swap TinyFlux for a TSDB with native retention (InfluxDB, Prometheus, TimescaleDB).
- Average every field uniformly, and accept the counter error as rounding.
- Store per-bucket _deltas_ for the counters instead of the running total.

**Why rejected:** Unbounded growth is the default failure and the whole reason the job exists. External `cron` splits the system's operational contract across two places and fails silently when a deploy forgets it. Delete-only would make month-old trends vanish entirely, when the actual requirement is _shape_ over long windows — an hourly average preserves that at a fraction of the size. A real TSDB has native retention but costs a daemon, its own memory footprint, and an operational dependency, which contradicts the resource-efficiency target this project is measured on; a ~100MB total footprint is only achievable with an embedded store. Uniform averaging is simpler by about ten lines and was what shipped first; it is not rounding error but a wrong number, and the cost of not fixing it is that the 24h and 7d network and CPU charts misreport bursts. Storing deltas would make the rolled-up points _more_ directly chartable, but it changes the field's meaning between tiers — the same key would mean "total" at `raw` and "increase" at `5m` — so every reader would need to know which tier it was looking at, and the frontend's one delta implementation would have to become two.

**Previously recorded here as an unfixed defect; now fixed.** Earlier revisions of this report documented the uniform averaging as a known correctness bug affecting the 24h and 7d views of `cpu_usage_ns`, `net_rx_bytes`, and `net_tx_bytes`. The classification above is the fix. It is covered by `TestFieldReductions` (per-field reductions, ordering, counter resets, absent readings) and `TestCounterRateFidelity` (rates reconstructed from rolled-up points, including the burst case that discriminates between the two reductions), plus an integration test that carries a counter through the real store and rollup and checks that traffic is conserved. A completeness test asserts every field `get_container_state()` returns appears in exactly one classification set — checked against the real function rather than a copied list, so adding a metric without deciding its reduction fails the suite instead of silently inheriting the averaging default.

---

### 7.7 — Terminal Safety

**Chose:** The exec endpoint (`POST /api/containers/{id}/exec`) requires `command` strictly as a **JSON array of strings** (`["ls", "-la"]`); single-string values are rejected with HTTP 400. The array is passed directly to `pylxd`'s `container.execute()` method as `argv` without shell interpolation. Containers are created with `security.privileged=false` enforced server-side in two independent layers: the resource layer builds the LXD config from a closed allowlist (`limits.memory`, `limits.cpu`, `boot.autostart`) so no caller-supplied dict is ever merged, and `create_container` then applies the key last so it overrides anything in the incoming `limits` — meaning it holds at the single point where every container is created, regardless of what a future caller does. Access is verified on every single execution request.

**Alternatives considered:**

- Command allowlist.
- Shell string input with escaping/sanitization.

**Why rejected:** Command allowlists restrict legitimate administrative use and create maintenance overhead. Sanitizing shell strings is notoriously error-prone due to varied shell escape sequences and metacharacters. The `argv` array approach completely eliminates shell injection at the protocol layer: no shell interpreter is invoked, meaning metacharacters (`;`, `|`, `&&`, `$()`) cannot be interpreted.

---

### 7.8 — Schema Behaviour on Rename and Delete-While-Assigned

**Chose:** Containers are keyed by a **UUID generated at creation**, never by the LXD container name. Names are user-facing and mutable; a rename therefore updates the `lxd_name` column on the same row, and every foreign key — assignments, audit entries, TinyFlux series — keeps pointing at an identifier that never changes. Had rows been keyed by name, a rename would either orphan a container's whole history or require a cascading rewrite across three stores.

Deleting an assigned container is a **soft delete**. `soft_delete_container()` stamps `deleted_at` and leaves the row in place, so the audit entry naming that container still resolves, and its metric history stays queryable. Reads filter on `deleted_at IS NULL`, so a deleted container disappears from the dashboard as expected.

Quota release works through that same filter rather than through the assignment: `compute_user_allocation()` walks the user's assignments and skips any container whose `deleted_at` is set, so deleting a container frees its RAM/CPU/disk against the owner's quota immediately. This is a **deliberate divergence from the plan**, which said the assignment row would be marked inactive on delete. It is not — the assignment stays `active = 1` and the `deleted_at` check on the container carries the semantics instead. The outcome is identical for both quota and dashboard, with one fewer write and no possibility of the two flags disagreeing; the cost is that "active assignment" alone no longer implies a live container, so every consumer of assignments must join to the container and check `deleted_at`. Both current consumers do.

LXD deletion happens **before** the database write, and a failure there raises 503 and returns without touching the row. The ordering matters: the reverse would leave a row marked deleted while the container still ran on the host, which is the one inconsistency that cannot be repaired by retrying.

**Alternatives considered:**

- Key rows by LXD container name.
- Hard-delete the container row with `ON DELETE CASCADE`.
- Hard-delete and rely on the audit log for history.

**Why rejected:** Name-keyed rows make rename a distributed rewrite. A cascading hard delete destroys exactly the data an accounting report needs — usage totals would silently shrink when a container was removed, and a reviewer asking "who was using what last month" would get an answer that is wrong rather than incomplete. The audit log records _that_ a delete happened, not the time series leading up to it, so it is not a substitute.

---

### 7.9 — How Much Data a Chart Needs, and How Much Reaches the Browser

**Chose:** `GET /api/containers/{id}/history?window=1h|24h|7d` maps each window to the pre-aggregated tier that already matches it, and returns that series:

```python
_WINDOW_MAP = {
    "1h":  (timedelta(hours=1), "raw"),
    "24h": (timedelta(hours=24), "5m"),
    "7d":  (timedelta(days=7),  "1h"),
}
```

A 24-hour window at the shipped 5-second interval is **17,280 raw points**; the `5m` tier serves the same day in **288**. The aggregation is not computed per request — it is a lookup of buckets §7.6's retention job already wrote, so the endpoint does no arithmetic and its cost does not grow with the window.

**The browser never aggregates**, and never receives points it would have to throw away. An unknown `window` is rejected with 400 listing the valid values rather than silently defaulting, so a typo cannot quietly return the wrong resolution. The response echoes `resolution` alongside the points so the chart can label what it is drawing instead of guessing from spacing. Like every metrics endpoint this reads only TinyFlux and never calls LXD, which is what keeps chart loads independent of hypervisor load and functional while LXD is down.

**Alternatives considered:**

- Ship raw points and downsample in JavaScript.
- Aggregate on demand at query time.
- Return a fixed point count with a server-side `limit`.

**Why rejected:** Shipping 17,280 points per container to build a few hundred pixels of chart wastes bandwidth and blocks the main thread on data the renderer immediately discards — and it scales with the number of tiles on the dashboard. On-demand aggregation re-derives every request what the retention job already computed once, making the 7-day view the slowest query in the system for no gain. A fixed `limit` bounds the payload but not the _span_ — the newest N points of a 7-day window is a few recent minutes, silently answering a different question than the one asked.

---

### 7.10 — Degraded Mode Behavior During Hypervisor Outages

**Chose:** When the LXD daemon is unreachable, the system enters a degraded mode:

- `/health` returns HTTP 200 with `{"status": "ok", "lxd_reachable": false}`.
- Read operations backed by SQLite or TinyFlux continue serving data normally, marking container status as `"Unknown"` and appending stale-data indicators.
- Write operations that require LXD (container creation, lifecycle changes, terminal exec) fail fast with **HTTP 503 Service Unavailable** and write no orphan database records.

**Alternatives considered:**

- Allowing handlers to crash with HTTP 500 and database tracebacks.
- Queuing write mutations offline for asynchronous replay.

**Why rejected:** Crashing with 500 hides hypervisor failure under generic application error codes. Queuing mutations offline risks desynchronization and unexpected execution once the daemon recovers. Failing fast with 503 accurately informs clients that the service is temporarily unavailable and safe to retry.

---

### 7.11 — Admin Bootstrap

**Chose:** The `ADMIN_BOOTSTRAP_EMAIL` environment variable. The first successful Google OAuth sign-in matching that exact email creates the initial Admin account. All other uninvited Google accounts receive an HTTP 403 Forbidden response.

**Alternatives considered:**

- "First sign-in wins" (first visitor becomes admin).

**Why rejected:** First-sign-in-wins contains a severe race condition: an accidental URL leak, web crawler, or unauthorized visitor hitting the callback first would acquire permanent admin rights. An explicit environment variable establishes an auditable, deterministic administrative identity.

---

### 7.12 — Reboot Survival and Process Supervision

**Chose:** Two separate `systemd` service units (`hsm-api.service` and `hsm-collector.service`) with `Restart=on-failure` and `RestartSec=5`, executing under a dedicated low-privilege system account (`hsm-runner`) belonging only to the `lxd` group, and both confined by the sandboxing directives described in §7.4.

**Alternatives considered:**

- Combined single process running API and collector together in threads.
- Process supervisor tools (Supervisor, PM2).

**Why rejected:** Merging API and collector into a single process creates failure coupling: a crash or memory leak in the metrics collector would take down the API. Using `systemd` relies on Linux standard process supervision without introducing third-party runtime dependencies.

---

## Issues Encountered and Solutions

### 1. TinyFlux TSDB In-Memory Index Invalidation Bug (Defect B7)

- **Problem:** During historical retention rollups, older downsampled points were written and raw points were subsequently deleted via `store.remove()`. TinyFlux 1.2.0 rebuilt its in-memory index from a partial state and marked `index.valid = True`. Subsequent time-range queries dropped recent points (returning 32 of 36 points on a 24h query or 0 of 90 on full sets).
- **Solution:** Implemented `backend/tsdb/store.py::remove_points()`, which wraps `store.remove()` and immediately forces `store.index.invalidate()`. All pruning passes in `collector/retention.py` route through this helper. Pinned `tinyflux==1.2.0` in `requirements.txt` and preserved a strict `xfail` test isolating the upstream library bug.

### 2. Concurrency Race Condition on User Resource Quotas (Defect B1)

- **Problem:** `check_quota` read current container allocations and `assignments.py` inserted the new assignment in separate, non-transactional database operations. Under concurrent requests, multiple threads read the identical pre-grant allocation, allowing users to exceed their quota ceiling (e.g. 2048MB allocated against a 1024MB quota).
- **Solution:** Wrapped assignment grants in `repo.transaction()` using SQLite `BEGIN IMMEDIATE` with a 5-second busy timeout. Mapped `repo.WriteLockTimeout` to HTTP 503 ("Server busy") in `app.py`, ensuring ACID isolation and deterministic quota bounds.

### 3. Duplicate User Invitation Race Returning HTTP 500 (Defect B2)

- **Problem:** Concurrent user invitations for the same email bypassed application checks and raised raw `sqlite3.IntegrityError` from the UNIQUE constraint, which Falcon caught and rendered as an unhandled HTTP 500 Internal Server Error.
- **Solution:** Encapsulated SQLite constraint errors inside `repo.py`, mapping UNIQUE violations to a domain `DuplicateKeyError`. Resource handlers catch `DuplicateKeyError` and return a clean HTTP 409 Conflict.

### 4. Astro Static Generation with Runtime Dynamic Routes

- **Problem:** Astro was configured for static output (`output: "static"`), which generates static HTML files at build time when container UUIDs are unknown.
- **Solution:** Configured [`frontend/src/pages/containers/[id].astro`](frontend/src/pages/containers/%5Bid%5D.astro) with `getStaticPaths()` returning a sentinel path (`/containers/view/`). At runtime, client scripts parse `?container=<uuid>` and dynamically fetch metadata and telemetry via `apiFetch`.

### 5. Optimistic Zero-Flicker Session Hydration

- **Problem:** Static Astro pages have no access to server cookies at build time. Relying purely on asynchronous `GET /api/auth/me` calls caused noticeable layout shifts and header flickering during page transitions.
- **Solution:** Engineered a dual-layer session management system in [`frontend/src/lib/session.ts`](frontend/src/lib/session.ts). On page load, `sessionStorage` synchronously hydrates user state for an instant paint, while an asynchronous background call to `GET /api/auth/me` validates the token and clears the cache if expired.

---

## What Was Learned

1. **Seam Depth in Integration Testing:** Shallow unit tests that mock high-level wrappers verify happy paths but hide interface bugs. Moving mock boundaries deeper—to the driver client layer (`_get_client`)—forced real unit conversions, error handling, and state normalization code to execute, exposing 7 critical defects that unit tests missed.
2. **Database Concurrency in SQLite:** SQLite's default deferred transactions allow multiple readers to upgrade to writers concurrently, creating race conditions. Using explicit `BEGIN IMMEDIATE` transactions provides strict serializability for allocation and quota accounting on a single-host architecture.
3. **Time-Series Rollup Lifecycle:** Writing multi-tier retention pipelines (raw → 5m → 1h) requires rigorous index management. Testing time-accelerated pipelines uncovered subtle caching and index invalidation issues that only emerge when data moves out of chronological order.
4. **Defense in Depth for Hypervisor Management:** Membership in the `lxd` group is effectively root-equivalent. Restricting hypervisor calls to a single client module (`backend/lxd/client.py`), running services under the low-privilege `hsm-runner` account, enforcing unprivileged containers server-side, and using list-form `argv` execution prevents privilege escalation at every layer.
5. **Static Frontend Architecture with React Islands:** Combining Astro static site generation with focused React islands delivered the responsiveness of a modern single-page app without requiring a Node.js SSR runtime on the host.

---

## Bonus Features Implemented

- **Sandboxed Systemd Service Units (`deploy/hsm-api.service`, `deploy/hsm-collector.service`):** `Restart=on-failure` under the dedicated low-privilege `hsm-runner` system account, confined with `NoNewPrivileges`, `PrivateTmp`, `ProtectHome` and `ProtectSystem=strict` plus explicit `ReadWritePaths` — `systemd-analyze security` scores **8.3** with these applied against **9.0 UNSAFE** without (§7.4).
- **Comprehensive Audit Trail (`audit_log` table):** Records all destructive and administrative actions (container delete, limit alterations, assignment grants/revocations, and terminal exec commands) with timestamps and user IDs.
- **Automated Multi-Tier Retention & Downsampling:** Hourly collector rollups downsample raw samples to 5m and 1h buckets — reduced per field, so counters stay differenceable and peaks stay peaks (§7.6) — and prune data older than 90 days, bounding TSDB growth.
- **Stale-Data Degraded Mode:** When LXD is unreachable, the API serves last-known database and TSDB points with `stale: true`, while the frontend `StatusBanner` notifies users of hypervisor degradation.
- **Live Chart Pre-Seeding (`/api/metrics/recent`):** Returns the last N raw points on page load so historical graphs render immediately without waiting for subsequent polling cycles.
- **Automated Resource Footprint Profiler (`scripts/measure_footprint.sh`):** Profiles RSS memory and CPU% over 60 seconds at idle to verify low overhead.

---

## Resource Measurements

**Measurement Conditions:** Both `hsm-api.service` and `hsm-collector.service` running under systemd as the `hsm-runner` user, with no active browser connections. One active LXD container was being monitored by the collector.

**Tool:** `scripts/measure_footprint.sh` — samples `ps -o rss,%cpu` for both process PIDs every 5 seconds over a 60-second window (12 samples).

**Command:**

```bash
./scripts/measure_footprint.sh
```

**Results (Averages over 60 seconds at idle):**

| Service                                             | RAM (RSS)     | CPU%      |
| --------------------------------------------------- | ------------- | --------- |
| API (`waitress-serve`)                              | 52.53 MB      | 0.13%     |
| Collector (`python -m backend.collector.collector`) | 50.84 MB      | 0.38%     |
| **Total**                                           | **103.37 MB** | **0.51%** |

_The collector process consumes slightly more CPU than the API at idle because it wakes up every 5 seconds to poll LXD and write time-series points._

> [!NOTE]
> These figures were measured with the collector running at the 5-second interval it ships with, so they describe the shipped configuration directly rather than being rescaled by arithmetic. Raising `COLLECTOR_INTERVAL_SECONDS` lowers the collector's CPU share roughly in proportion, since the interval changes how often the loop runs; the RAM figures are unaffected, because it changes nothing about what the loop allocates.

---

## Known Limitations

- **Single-Host Architecture:** Targets a single Linux server running a local LXD daemon; no multi-node cluster orchestration.
- **Request/Response Terminal:** Interactive terminal operates via request/response command execution (`argv` array) rather than a persistent bidirectional PTY/WebSocket stream.
- **Invitation via Direct Google OAuth:** Inviting a user creates their record in SQLite; the user accesses the platform upon authenticating with that exact Google email (no external SMTP server required).
- **Static Page Navigation:** Uses Astro static routing with React islands rather than an overarching client-side SPA router.

### Known Gaps

Distinct from the non-goals above, these are places where the shipped behaviour is narrower than it first appears.

**`limit_disk_gb` is an allocation ledger, not an enforced quota.** Disk is validated, counted against `quota_disk_gb` on create and grant, and reported by `/api/accounting`, so the system correctly refuses to over-commit disk on paper. It does not bound the root filesystem: `create_container` builds its LXD config from `limits.memory` and `limits.cpu` only, and choosing a storage pool sets `devices.root.pool` with no `size` key. A container can fill the host disk while sitting within its nominal allocation. RAM and CPU limits are genuinely enforced by LXD; disk is the one limit that is bookkeeping.

Setting the `size` key would not fix this, and measurement is why. On this host the only pool uses the `dir` driver, which accepts `size=2GB` silently and ignores it — `lxc config device show` reports the size while `df` inside the container reports the host's full 1007GB, with no error or warning at any point. Applying the key unconditionally would make both `lxc config` and the dashboard display a quota that does not exist, converting a visible limitation into a false guarantee. Honest enforcement requires reading `driver` from `list_storage_pools()`, applying `size` only on a size-capable pool (`btrfs`, `zfs`, `lvm`, `ceph`), and refusing the request otherwise — plus a migration path for containers already created without one. That is a behaviour change with a capability matrix behind it, so it is documented rather than half-implemented.

**Refresh tokens are not rotated.** Reuse is deliberate, and the trade-off against multi-tab reliability is recorded there.

---

## AI Tool Usage

**Tool Used:** Google Antigravity, Anthropic Claude Code.

**Workflow Integration:**

- Implemented backend modules (schema, repo, auth, resources, LXD client, collector, retention) following the build guide.
- Implemented frontend architecture (Astro pages, React islands, design system, session hydration).
- Generated deep integration test suites (`test_route_coverage.py`, `test_lifecycle.py`, `test_security.py`, `test_resilience.py`, `test_pipeline.py`, `test_integrity.py`) and diagnosed defects.
- Authored production systemd units, deployment runbooks, and technical documentation.

**Verification & Defense:**

- Every architectural decision, SQL query, transaction boundary, and TSDB workaround was reviewed and validated against test suites.
- The test suite of **378 passing tests** provides automated proof that all security, data integrity, and resilience invariants hold.
