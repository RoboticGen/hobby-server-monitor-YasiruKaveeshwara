"""
Health check endpoint.

Returns a simple status indicator so operators and the frontend can
confirm the API process is alive and responding.

NOTE: This is a stub returning a static "ok". Phase 14 (Step 14.1) will
extend this to also check LXD reachability, returning either
{"status": "ok", "lxd_reachable": true} or
{"status": "degraded", "lxd_reachable": false}.
"""

import falcon


class HealthResource:
    """Responds to GET /health with a JSON status object.

    Currently a minimal liveness check. Will be extended in Phase 14 to
    include an LXD connectivity probe, so the frontend can show a
    degraded-service banner when LXD is unreachable.
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return a 200 JSON response indicating the API is alive."""
        resp.media = {"status": "ok"}
        resp.status = falcon.HTTP_200
