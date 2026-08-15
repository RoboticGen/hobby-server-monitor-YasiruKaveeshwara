"""
Falcon application entrypoint.

Assembles the WSGI app, registers middleware, and wires up all route
resources. This is the single place where the app's component graph is
built — no route registration happens anywhere else.
"""

import subprocess
import sys
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server
import logging

import falcon

from backend.auth.middleware import AuthMiddleware
from backend.config import config
from backend.db import repo
from backend.resources.accounting import AccountingResource
from backend.resources.auth import (
    GoogleCallbackResource,
    GoogleLoginResource,
    LogoutResource,
    MeResource,
    RefreshResource,
)
from backend.resources.assignments import (
    AssignmentResource,
    ContainerDetailByUserResource,
)
from backend.resources.containers import ContainerListResource, LxdOptionsResource
from backend.resources.health import HealthResource
from backend.resources.metrics import (
    ContainerHistoryResource,
    LatestMetricsResource,
    RecentMetricsResource,
)
from backend.resources.terminal import ContainerExecResource
from backend.resources.users import UserDetailResource, UserListResource


class _ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    """Dev server that handles each connection on its own thread.

    wsgiref's stock WSGIServer serves one connection at a time and does not
    hang up on an idle keep-alive connection. A browser holds its connection
    open after loading a page, so the next request queues behind it and never
    gets served — the tab just spins.

    That shows up first on GET /api/auth/google/login, because it is a
    top-level navigation on a fresh connection rather than an XHR that
    completes and frees its socket, and because its handler only builds a
    redirect string, so a hang there cannot be the handler's own fault.

    daemon_threads=True so a parked browser connection cannot keep the
    process alive after Ctrl-C.
    """

    daemon_threads = True


def _handle_write_lock_timeout(req, resp, ex, params):
    """Render repo.WriteLockTimeout as a retryable 503."""
    raise falcon.HTTPServiceUnavailable(
        title="Server busy",
        description="The server is handling another change to this data. "
        "Please retry.",
    )


def create_app() -> falcon.App:
    """Create and configure the Falcon WSGI application.

    Constructs a falcon.App instance and registers all route resources.
    Returns the configured app instance, ready to be served by any WSGI
    server (wsgiref for dev, waitress for production).
    """
    # AuthMiddleware decodes the JWT cookie and sets req.context.user
    # on every request. It does NOT reject unauthenticated requests —
    # individual resource methods call require_role() or
    # require_container_access() to enforce their own requirements.
    #
    # CORSMiddleware is listed FIRST, which matters: Falcon unwinds response
    # middleware in reverse order, so being first means its process_response
    # runs last and the CORS headers land on *every* reply, including error
    # replies raised from deeper middleware or resource code. Without that, a
    # 401 from require_role() reaches the browser stripped of CORS headers,
    # the browser refuses to expose the response, and the frontend cannot
    # tell "not signed in" (redirect to /login) from "server unreachable"
    # (show an error) — the exact distinction index.astro depends on.
    #
    # allow_origins/allow_credentials are pinned to the single configured
    # origin rather than "*" for two reasons: the wildcard is illegal in a
    # credentialed CORS response and browsers reject it outright, and echoing
    # only the known frontend origin keeps arbitrary sites from making
    # cookie-bearing calls to this API on a signed-in user's behalf.
    app = falcon.App(
        middleware=[
            falcon.CORSMiddleware(
                allow_origins=config.frontend_origin,
                allow_credentials=config.frontend_origin,
            ),
            AuthMiddleware(),
        ]
    )

    # Contended writes are a capacity problem, not a bug. repo.transaction()
    # serialises quota-critical writes on SQLite's write lock, so a caller
    # that waits out the busy timeout has hit load, not a broken server —
    # 503 tells it to retry, where a 500 would say "stop, this is broken".
    # Registered centrally so no transactional endpoint can forget it.
    app.add_error_handler(repo.WriteLockTimeout, _handle_write_lock_timeout)

    # --- Route registration ------------------------------------------
    # Each route is added in the phase that builds its resource class.

    # health check
    app.add_route("/health", HealthResource())

    # authentication routes
    app.add_route("/api/auth/google/login", GoogleLoginResource())
    app.add_route("/api/auth/google/callback", GoogleCallbackResource())
    app.add_route("/api/auth/me", MeResource())
    # Redeems the refresh token for a new access token. Not in the route
    # coverage test's PUBLIC set on purpose: it 401s without a valid refresh
    # cookie, so it satisfies the "every route rejects anonymous callers" rule
    # on its own terms rather than by exemption.
    app.add_route("/api/auth/refresh", RefreshResource())
    app.add_route("/api/auth/logout", LogoutResource())

    # container list + create
    app.add_route("/api/containers", ContainerListResource())

    # host options backing the creation form's dropdowns (admin-only).
    # Degrades to empty lists with stale=true when LXD is unreachable.
    app.add_route("/api/lxd/options", LxdOptionsResource())

    # user management
    app.add_route("/api/users", UserListResource())
    app.add_route("/api/users/{user_id}", UserDetailResource())

    # assignment grant/revoke + container detail (GET by user, PATCH/DELETE by admin)
    app.add_route(
        "/api/users/{user_id}/containers/{container_id}",
        AssignmentResource(),
    )
    app.add_route("/api/containers/{container_id}", ContainerDetailByUserResource())

    # metrics: latest (dashboard polling target) + per-container history.
    # Both read from TinyFlux only, never LXD.
    app.add_route("/api/metrics/latest", LatestMetricsResource())
    app.add_route("/api/containers/{container_id}/history", ContainerHistoryResource())
    # recent: last N raw points for live-graph pre-seeding on page load.
    app.add_route("/api/metrics/recent", RecentMetricsResource())

    # terminal: run one command inside a container (injection-safe, audited).
    # Available to any user with an active assignment, not just admins.
    app.add_route("/api/containers/{container_id}/exec", ContainerExecResource())

    # accounting: host capacity vs allocated, plus per-user allocation vs
    # quota. Degrades to DB-only figures when LXD is unreachable.
    app.add_route("/api/accounting", AccountingResource())

    return app


if __name__ == "__main__":
    # -----------------------------------------------------------------
    # Development-only server using Python's built-in wsgiref, with the
    # threading server class above — the stock one serialises connections
    # and deadlocks against browser keep-alive. Production deployments use
    # waitress-serve under systemd (Phase 24), which is threaded already.
    # This block exists solely for quick local iteration during dev.
    # -----------------------------------------------------------------
    app = create_app()

    # Start the collector as a separate OS process in dev so the UI has a
    # real background sample source without sharing the API process.
    repo_root = Path(__file__).resolve().parent.parent
    collector_proc = subprocess.Popen(
        [sys.executable, "-m", "backend.collector.collector"],
        cwd=str(repo_root),
    )

    # Without this, logging defaults to WARNING on the root logger with no
    # handler configured, so the log.error() calls in resources/auth.py that
    # carry Google's OAuth failure reason would be discarded — and that reason
    # exists nowhere else. Same format as the collector, for greppable output.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print(f"[dev] Starting Falcon on 0.0.0.0:{config.port} ...")
    with make_server("0.0.0.0", config.port, app, _ThreadedWSGIServer) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[dev] Server stopped.")
        finally:
            collector_proc.terminate()
            try:
                collector_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                collector_proc.kill()
