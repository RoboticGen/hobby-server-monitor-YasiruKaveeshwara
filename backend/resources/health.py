"""
Health check endpoint.

Returns a JSON status object so operators and the frontend can confirm
the API process is alive and determine whether LXD is reachable.

Phase 14.1 — This endpoint now probes LXD connectivity on every call.
It always returns HTTP 200; it must NEVER fail or hang just because LXD
is down. That is the entire point of this endpoint — it is the one place
the frontend can check to determine whether the system is operating
normally ("ok") or in degraded mode ("degraded") and show an appropriate
banner to the user.

Decision 7.10 (from PROJECT-PLAN.md): the UI/API must degrade gracefully
when LXD is unreachable, rather than crash or hang.
"""

import falcon

from backend.lxd.client import check_lxd_reachable


class HealthResource:
    """Responds to GET /health with a JSON status object.

    Probes LXD reachability via lxd.client.check_lxd_reachable() which
    performs a lightweight GET /1.0 call. If that call times out or
    throws, check_lxd_reachable() returns False without raising — it
    was specifically designed to never raise, since a crash here would
    hide the real issue (LXD being down) behind a 500 error.
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return a 200 JSON response with API and LXD status.

        Always returns HTTP 200 — even when LXD is unreachable. The
        response body indicates the actual state:
        - {"status": "ok",       "lxd_reachable": true}  — all systems go
        - {"status": "degraded", "lxd_reachable": false} — API is up,
          LXD is down; dashboard should show a degraded-service banner.
        """
        lxd_ok = check_lxd_reachable()

        resp.media = {
            "status": "ok" if lxd_ok else "degraded",
            "lxd_reachable": lxd_ok,
        }
        # Always 200 — this endpoint itself should never fail. The
        # difference between ok and degraded is in the response body,
        # not the status code.
        resp.status = falcon.HTTP_200
