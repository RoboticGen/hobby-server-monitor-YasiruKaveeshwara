# Production Deployment Guide (Systemd Setup)

Follow these exact steps to configure, install, and run the **Hobby Server Monitor** backend and collector as production `systemd` services on your host machine.

---

## 1. Architecture Overview

In production, two separate systemd services run independently under a dedicated low-privilege system account:

| Service Unit            | Entrypoint                                                 | Responsibility                                                             |
| ----------------------- | ---------------------------------------------------------- | -------------------------------------------------------------------------- |
| `hsm-api.service`       | `waitress-serve --port=8000 --call backend.app:create_app` | Production WSGI HTTP server; handles API requests, auth, and mutations     |
| `hsm-collector.service` | `python -m backend.collector.collector`                    | Independent polling daemon; writes TinyFlux metrics and runs hourly rollup |

Both services are configured with `Restart=on-failure` to ensure automatic recovery after an unexpected crash or system reboot.

---

## 2. Step-by-Step Setup

### Step 1: Set Project Environment Variable

Run commands from the repository root, or define `PROJECT_DIR` pointing to your installation path:

```bash
# Set PROJECT_DIR to your repository root (e.g. current directory or /opt/hobby-server-monitor)
PROJECT_DIR="$(pwd)"
```

### Step 2: Create the Dedicated Service Account

As per security decision 7.4, the backend and collector run as a dedicated, low-privilege system user that belongs to the `lxd` group and nothing else.

```bash
# Create low-privilege system user without login shell or home directory
sudo useradd --system --no-create-home hsm-runner

# Add user to the lxd group so it can interact with the LXD daemon socket
sudo usermod -aG lxd hsm-runner
```

### Step 3: Configure File & Directory Permissions

The `hsm-runner` user needs permission to access the project directory and write to the `data/` directory (where SQLite `app.db` and TinyFlux `metrics.tinyflux` reside).

```bash
# If the project is in a home directory, ensure the parent directory is traversable
chmod o+x "$HOME"

# Ensure the data directory exists
mkdir -p "$PROJECT_DIR/data"

# Transfer ownership of data directory to hsm-runner
sudo chown -R hsm-runner:hsm-runner "$PROJECT_DIR/data"
```

### Step 4: Configure Environment Variables

Verify that `backend/.env` exists and contains all required production secrets:

```bash
cd "$PROJECT_DIR"
cp backend/.env.example backend/.env   # if not already created
nano backend/.env
```

Ensure the following production settings are configured:

- `SESSION_COOKIE_SECURE=true` (if serving frontend over HTTPS)
- `JWT_SECRET=<long-random-secret>`
- `GOOGLE_CLIENT_ID` & `GOOGLE_CLIENT_SECRET` (configured with production redirect URI)
- `GOOGLE_REDIRECT_URI` matches your domain, e.g. `https://your-domain.com/api/auth/google/callback`
- `FRONTEND_ORIGIN` matches your frontend domain, e.g. `https://your-domain.com`
- `PORT=8000`

### Step 5: Initialize the Database

Run database initialization once to create tables and schema:

```bash
cd "$PROJECT_DIR"
source .venv/bin/activate
python -m backend.db.init_db

# Ensure the initialized database is owned by hsm-runner
sudo chown -R hsm-runner:hsm-runner "$PROJECT_DIR/data"
```

### Step 6: Install Systemd Service Units

Copy the unit files to `/etc/systemd/system/` and configure the working directory to match your `$PROJECT_DIR`:

```bash
# Copy unit files
sudo cp "$PROJECT_DIR/deploy/hsm-api.service" /etc/systemd/system/hsm-api.service
sudo cp "$PROJECT_DIR/deploy/hsm-collector.service" /etc/systemd/system/hsm-collector.service

# Update WorkingDirectory, EnvironmentFile, and ExecStart to your actual project path
sudo sed -i "s|/opt/hobby-server-monitor|$PROJECT_DIR|g" /etc/systemd/system/hsm-api.service
sudo sed -i "s|/opt/hobby-server-monitor|$PROJECT_DIR|g" /etc/systemd/system/hsm-collector.service

# Reload systemd daemon to recognize updated unit files
sudo systemctl daemon-reload
```

The same `sed` also rewrites the `ReadWritePaths=` line for the data directory,
since it contains the placeholder path. Nothing else needs editing.

#### What the sandboxing directives do, and what they will break

Both units are confined rather than trusted, because `hsm-runner` belongs to the
`lxd` group — and anyone who can talk to the LXD socket can start a privileged
container that bind-mounts the host filesystem. Group membership alone is
therefore close to root, so the units restrict what that authority can reach:

| Directive              | Effect                                                      |
| ---------------------- | ----------------------------------------------------------- |
| `NoNewPrivileges=yes`  | No setuid/setcap escalation from inside the service         |
| `PrivateTmp=yes`       | Private `/tmp`, invisible to and from other services        |
| `ProtectSystem=strict` | **Entire** filesystem read-only except `ReadWritePaths`     |
| `ProtectHome=yes`      | `/home`, `/root` and `/run/user` are empty to the service   |
| `RestartSec=5`         | Waits 5s between restarts instead of hammering a broken dep |

`ProtectSystem=strict` is the one that bites. It mounts everything read-only,
so any path the service writes must be listed back explicitly:

```ini
ReadWritePaths=/opt/hobby-server-monitor/data
ReadWritePaths=/var/snap/lxd/common/lxd
```

Two failure modes to recognise, because neither error message points at the
mount:

- **`sqlite3.OperationalError: attempt to write a readonly database`**, or
  TinyFlux failing with `EROFS`/permission errors. The data directory is not in
  `ReadWritePaths`. This is _not_ an ownership problem — `chown` will not fix
  it, because the read-only mount refuses the write before file permissions are
  consulted.
- **LXD connection failures.** Connecting to an `AF_UNIX` socket counts as a
  write on the socket inode, so the socket's directory needs to be writable too.
  The path above is for **snap-installed LXD**. On a `deb` install the socket
  lives at `/var/lib/lxd/unix.socket`, so change that line to
  `ReadWritePaths=/var/lib/lxd` and update `LXD_ENDPOINT` to match.

Verify the confinement took effect, and see what is still exposed:

```bash
systemd-analyze security hsm-api.service hsm-collector.service
```

### Step 7: Enable & Start the Services

Enable both services so they start automatically across system reboots:

```bash
# Enable and start API service
sudo systemctl enable hsm-api.service
sudo systemctl start hsm-api.service

# Enable and start Collector service
sudo systemctl enable hsm-collector.service
sudo systemctl start hsm-collector.service
```

### Step 8: Verify Service Health & Connectivity

Check that both services are active and running:

```bash
sudo systemctl status hsm-api.service
sudo systemctl status hsm-collector.service
```

Smoke test the API health probe:

```bash
curl -s http://localhost:8000/health
# Expected output: {"status": "ok", "lxd_reachable": true}
```

---

## 3. Frontend Production Build & Hosting

The frontend is a static Astro build:

```bash
cd "$PROJECT_DIR/frontend"
npm install
npm run build
# Emits static assets to frontend/dist/
```

Deploy the contents of `frontend/dist/` behind your web server (Nginx, Caddy, or Apache) with reverse proxying for `/api/` and `/health` to `http://127.0.0.1:8000`.

---

## 4. Measuring Resource Footprint

Run the automated profiling script to verify that idle RSS memory and CPU consumption meet efficiency baselines:

```bash
cd "$PROJECT_DIR"
chmod +x scripts/measure_footprint.sh
./scripts/measure_footprint.sh
```

**Expected idle results (over 60 seconds):**

- API Service (`waitress-serve`): ~52 MB RAM, ~0.1% CPU
- Collector Service (`python collector.py`): ~50 MB RAM, ~0.4% CPU
- Total Combined: **~103 MB RAM, ~0.5% CPU**

---

## 5. Troubleshooting & Maintenance

### Inspect Logs in Real-Time

```bash
# API logs
sudo journalctl -u hsm-api.service -f

# Collector logs
sudo journalctl -u hsm-collector.service -f
```

### Restart Services After Code Updates

```bash
sudo systemctl restart hsm-api.service
sudo systemctl restart hsm-collector.service
```

### Checking Socket Permissions

If `GET /health` reports `lxd_reachable: false`:

1. Check if `lxd` daemon is active: `sudo systemctl status snap.lxd.daemon` or `sudo systemctl status lxd`
2. Test socket access directly as `hsm-runner`:
   ```bash
   sudo -u hsm-runner lxc list
   ```
