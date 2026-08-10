"""
Metrics API endpoints.

Serves the dashboard's live tile polling (`/api/metrics/latest`) and the
per-container history charts (`/api/containers/{id}/history`). Both read
exclusively from TinyFlux — they NEVER call backend.lxd.client directly.

Dashboard traffic (however many tabs are open) must never add load to the LXD daemon. The collector process is the
only thing that polls LXD, on a fixed 10-second timer; these endpoints just
serve whatever the collector last wrote. Reading from TinyFlux also means
these endpoints keep working (serving last-known data) even when LXD is slow or down.
"""

from datetime import datetime, timedelta, timezone

import falcon

from backend.auth.middleware import require_container_access
from backend.tsdb import store

# Maps the user-facing window to (time span, TinyFlux resolution tag).
# This is decision 7.9: a 24h/7d chart is served from the already
# downsampled buckets the retention job produced (§7.6), never from raw
# 10-second points — the browser never receives 8,640 raw points for a day.
_WINDOW_MAP: dict[str, tuple[timedelta, str]] = {
    "1h": (timedelta(hours=1), "raw"),
    "24h": (timedelta(hours=24), "5m"),
    "7d": (timedelta(days=7), "1h"),
}


class LatestMetricsResource:
    """GET /api/metrics/latest?container=<id> — most recent sample.

    The frontend's MetricTile polls this every 10 seconds while visible.
    It reads the newest raw TinyFlux point and returns it, and it never
    touches backend.lxd.client (dashboard polling cost
    must be independent of LXD load and survive LXD being down).
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return the latest metric point for the requested container."""
        # Explicit 401 for unauthenticated callers before any other check,
        # so a missing 'container' param can't mask an auth failure as a 400.
        if req.context.user is None:
            raise falcon.HTTPUnauthorized(
                title="Not authenticated",
                description="Please sign in to view metrics.",
            )

        container_id = req.get_param("container")
        if not container_id:
            raise falcon.HTTPBadRequest(
                title="Missing container parameter",
                description="Provide a 'container' query parameter with the "
                "container's id, e.g. /api/metrics/latest?container=<id>.",
            )

        # Re-check access on every call — never cached (same pattern the
        # terminal exec endpoint depends on in Phase 12).
        require_container_access(req, container_id)

        # Read the newest raw point straight from TinyFlux. No LXD call.
        latest = store.get_latest_point(container_id)
        if latest is None:
            # No samples collected yet for this container (e.g. just created
            # and the collector hasn't run a cycle). Return an explicit empty
            # payload rather than 404 so the tile can render a "no data yet".
            resp.media = {"container_id": container_id, "point": None}
            return

        resp.media = {"container_id": container_id, "point": latest}


class ContainerHistoryResource:
    """GET /api/containers/{id}/history?window=1h|24h|7d — aggregated series.

    Maps the requested window to the matching pre-aggregated resolution
    and returns that already-downsampled series from
    TinyFlux. Like the latest endpoint, it never calls LXD directly.
    """

    def on_get(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        container_id: str,
    ) -> None:
        """Return an aggregated metric series for the given window."""
        # Authorization is re-checked on every call; raises 401 if there is
        # no authenticated user, 403 if the user lacks access.
        require_container_access(req, container_id)

        # Default to the 1-hour window if none is supplied.
        window = req.get_param("window", default="1h")
        if window not in _WINDOW_MAP:
            raise falcon.HTTPBadRequest(
                title="Invalid window",
                description="The 'window' parameter must be one of: "
                f"{sorted(_WINDOW_MAP)}.",
            )

        span, resolution = _WINDOW_MAP[window]
        end = datetime.now(tz=timezone.utc)
        start = end - span

        # Query the already-aggregated series for this resolution. The
        # browser receives the downsampled points, not raw 10s samples.
        points = store.query_range(
            container_id, start=start, end=end, resolution=resolution
        )

        resp.media = {
            "container_id": container_id,
            "window": window,
            "resolution": resolution,
            "points": points,
        }
