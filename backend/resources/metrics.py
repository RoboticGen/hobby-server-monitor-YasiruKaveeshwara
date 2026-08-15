"""
Metrics API endpoints.

Serves the dashboard's live tile polling (`/api/metrics/latest`) and the
per-container history charts (`/api/containers/{id}/history`). Both read from
TinyFlux rather than from LXD.

Dashboard traffic (however many tabs are open) must never add load to the LXD
daemon. The collector process is the only thing that polls LXD on a timer;
these endpoints just serve whatever the collector last wrote. Reading from
TinyFlux also means these endpoints keep working (serving last-known data)
even when LXD is slow or down.

There is exactly one bounded exception, `/api/metrics/latest?fresh=1`, which
takes a single live sample for a container that does not yet have the two
points a rate can be computed from. It is gated on the stored point count
rather than on the caller, so it can fire at most twice per container per
lifetime and cannot be used to put dashboard load on LXD. See
LatestMetricsResource for the full reasoning.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import falcon

from backend.auth.middleware import require_container_access
from backend.collector import sampler
from backend.collector.retention import (
    _FIVE_MIN_BUCKET,
    _ONE_HOUR_BUCKET,
    _bucket_key,
    _reduce_fields,
)
from backend.db import repo
from backend.tsdb import store

# Maps the user-facing window to (time span, TinyFlux resolution tag).
# This is decision 7.9: a 24h/7d chart is served from the already
# downsampled buckets the retention job produced (§7.6), never from raw
# points — the browser never receives a full day of them (17,280 at the
# shipped 5-second interval).
_WINDOW_MAP: dict[str, tuple[timedelta, str]] = {
    "1h": (timedelta(hours=1), "raw"),
    "24h": (timedelta(hours=24), "5m"),
    "7d": (timedelta(days=7), "1h"),
}


class _PointProxy:
    """Wrapper so raw dicts work with retention's _reduce_fields helper."""

    def __init__(self, ts: datetime, fields: dict):
        self.time = ts
        self.fields = fields


def _rollup_raw_points(
    raw_dicts: list[dict], bucket_width: timedelta, resolution: str, container_id: str
) -> list[dict]:
    """Aggregate raw points on the fly when the stored downsampled tier is empty."""
    if not raw_dicts:
        return []

    buckets: dict[datetime, list[_PointProxy]] = defaultdict(list)
    for p in raw_dicts:
        try:
            ts = datetime.fromisoformat(p["time"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            b_ts = _bucket_key(ts, bucket_width)
            fields = {
                k: v
                for k, v in p.items()
                if k not in ("time", "container_id", "resolution")
            }
            buckets[b_ts].append(_PointProxy(ts, fields))
        except Exception:
            continue

    results = []
    for b_ts in sorted(buckets.keys()):
        reduced = _reduce_fields(buckets[b_ts])
        if reduced:
            results.append(
                {
                    "time": b_ts.isoformat(),
                    "container_id": container_id,
                    "resolution": resolution,
                    **reduced,
                }
            )
    return results


class LatestMetricsResource:
    """GET /api/metrics/latest?container=<id>[&fresh=1] — most recent sample.

    Normally reads the newest raw TinyFlux point and never touches
    backend.lxd.client, because dashboard polling cost must be independent
    of LXD load and must keep working while LXD is down.

    `fresh=1` is a narrow, self-limiting exception to that rule. A container
    with fewer than two raw points cannot yet be charted: RAM and disk are
    gauges and need one point, but CPU and network are counters, so a *rate*
    needs two points to difference. Until the collector has ticked twice the
    live view has nothing to draw, which is up to two collector intervals of
    an empty graph right after a container starts.

    When `fresh=1` is passed AND the container is below that two-point floor,
    this takes one live LXD sample and persists it, so the client reaches a
    chartable state in a round trip instead of in two collector intervals.
    Once two points exist the parameter is ignored and the endpoint is
    TSDB-only again, which is what keeps steady-state polling off LXD: the
    cost is bounded to the first two samples of a container's life, no matter
    how often a client asks or how many clients ask.
    """

    #: A rate needs two points to difference; below this the live view is blank.
    _CHARTABLE_MINIMUM = 2

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

        # Cold-start path: only for a container that cannot be charted yet,
        # and only when the client asked. The point count is what gates the
        # LXD call, not the caller, so this cannot be used to generate load.
        if req.get_param_as_bool("fresh", blank_as_true=True, default=False):
            existing = store.get_recent_points(
                container_id, limit=self._CHARTABLE_MINIMUM
            )
            if len(existing) < self._CHARTABLE_MINIMUM:
                container = repo.get_container_by_id(container_id)
                if container is not None:
                    sampled = sampler.try_sample_container(
                        container, reason="cold-start"
                    )
                    if sampled is not None:
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
        # browser receives the downsampled points, not raw samples.
        points = store.query_range(
            container_id, start=start, end=end, resolution=resolution
        )

        # Dynamic fallback: if a downsampled tier (5m or 1h) has no points
        # stored yet because the retention pass has not run, roll up whatever
        # higher-resolution points are available so the client gets real data.
        if not points and resolution == "5m":
            raw_points = store.query_range(
                container_id, start=start, end=end, resolution="raw"
            )
            points = _rollup_raw_points(
                raw_points,
                _FIVE_MIN_BUCKET,
                resolution="5m",
                container_id=container_id,
            )
        elif not points and resolution == "1h":
            intermediate = store.query_range(
                container_id, start=start, end=end, resolution="5m"
            )
            if not intermediate:
                intermediate = store.query_range(
                    container_id, start=start, end=end, resolution="raw"
                )
            points = _rollup_raw_points(
                intermediate,
                _ONE_HOUR_BUCKET,
                resolution="1h",
                container_id=container_id,
            )

        resp.media = {
            "container_id": container_id,
            "window": window,
            "resolution": resolution,
            "points": points,
        }


class RecentMetricsResource:
    """GET /api/metrics/recent?container=<id>&limit=<n> — last N raw points.

    Returns the most recent `limit` raw collector samples for a container
    in ascending-time order so the frontend can immediately pre-populate
    the live graph on first load without waiting to accumulate points from
    the 5-second polling loop one-by-one.

    Like all metrics endpoints, this NEVER calls LXD — it only reads from
    TinyFlux (decision 7.5: dashboard polling cost must not add LXD load).
    """

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        """Return the last `limit` raw points for the requested container."""
        # Auth check first — same pattern as LatestMetricsResource.
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
                "container's id, e.g. /api/metrics/recent?container=<id>.",
            )

        # Re-check access on every call — never cached.
        require_container_access(req, container_id)

        # `limit` caps how many recent raw points to return.
        # Default 60 ≈ 5 minutes at a 5-second collector interval, enough
        # to paint a useful live chart immediately on page load.
        try:
            limit = int(req.get_param("limit") or "60")
            if limit < 1 or limit > 720:  # cap at 720 = 1h at 5s interval
                limit = 60
        except ValueError:
            limit = 60

        # Read from TinyFlux — no LXD call (decision 7.5).
        points = store.get_recent_points(container_id, limit=limit)

        resp.media = {
            "container_id": container_id,
            "points": points,
            "count": len(points),
        }
