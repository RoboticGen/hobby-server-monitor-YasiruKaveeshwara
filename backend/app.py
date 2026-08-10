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
from backend.resources.auth import (
    GoogleCallbackResource,
    GoogleLoginResource,
    LogoutResource,
    MeResource,
)
from backend.resources.containers import (
    ContainerDetailResource,
    ContainerListResource,
)
from backend.resources.health import HealthResource


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

    # container detail (update/delete)
    app.add_route("/api/containers/{container_id}", ContainerDetailResource())

    # Future routes (registered as their resource modules are built):
    # Phase 8.1:  /api/users
    # Phase 11.1: /api/metrics/*
    # Phase 12.1: /api/containers/{id}/exec
    # Phase 13.1: /api/accounting

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
