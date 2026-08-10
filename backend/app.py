"""
Falcon application entrypoint.

Assembles the WSGI app, registers middleware, and wires up all route
resources. This is the single place where the app's component graph is
built — no route registration happens anywhere else.
"""

from wsgiref.simple_server import make_server

import falcon

from backend.auth.middleware import AuthMiddleware
from backend.config import config
from backend.resources.accounting import AccountingResource
from backend.resources.auth import (
    GoogleCallbackResource,
    GoogleLoginResource,
    LogoutResource,
    MeResource,
)
from backend.resources.assignments import (
    AssignmentResource,
    ContainerDetailByUserResource,
)
from backend.resources.containers import ContainerListResource
from backend.resources.health import HealthResource
from backend.resources.metrics import (
    ContainerHistoryResource,
    LatestMetricsResource,
)
from backend.resources.terminal import ContainerExecResource
from backend.resources.users import UserDetailResource, UserListResource


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
    app = falcon.App(middleware=[AuthMiddleware()])

    # --- Route registration ------------------------------------------
    # Each route is added in the phase that builds its resource class.

    # health check
    app.add_route("/health", HealthResource())

    # authentication routes
    app.add_route("/api/auth/google/login", GoogleLoginResource())
    app.add_route("/api/auth/google/callback", GoogleCallbackResource())
    app.add_route("/api/auth/me", MeResource())
    app.add_route("/api/auth/logout", LogoutResource())

    # container list + create
    app.add_route("/api/containers", ContainerListResource())


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
    app.add_route(
        "/api/containers/{container_id}/history", ContainerHistoryResource()
    )

    # terminal: run one command inside a container (injection-safe, audited).
    # Available to any user with an active assignment, not just admins.
    app.add_route(
        "/api/containers/{container_id}/exec", ContainerExecResource()
    )

    # accounting: host capacity vs allocated, plus per-user allocation vs
    # quota. Degrades to DB-only figures when LXD is unreachable.
    app.add_route("/api/accounting", AccountingResource())

    return app


if __name__ == "__main__":
    # -----------------------------------------------------------------
    # Development-only server using Python's built-in wsgiref.
    # Production deployments use waitress-serve under systemd (Phase 24),
    # which provides proper process management and worker handling.
    # This block exists solely for quick local iteration during dev.
    # -----------------------------------------------------------------
    app = create_app()
    print(f"[dev] Starting Falcon on 0.0.0.0:{config.port} ...")
    with make_server("0.0.0.0", config.port, app) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[dev] Server stopped.")
