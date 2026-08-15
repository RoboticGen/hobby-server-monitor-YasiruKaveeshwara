"""
One definition of "take a raw metric sample and store it".

The background collector calls this on its fixed interval, and the API calls
it at two other moments where waiting for the next collector tick would show
the user an empty graph:

- immediately after a container create or a state action (start/restart/
  unfreeze), so a container that has just come up has a data point before the
  browser asks for one;
- on an explicit cold-start request from the live view, for a container that
  does not yet have the two points a rate needs.

Keeping this in one place matters because a "sample" is not just an LXD read:
it is an LXD read written to TinyFlux under a specific container_id and
resolution tag. Two copies of that pairing would drift, and the retention job
and history endpoint both depend on the tags being exactly right.

Sampling is deliberately split into a raising and a non-raising form. The
collector loop wants the exception so it can log which container failed and
continue; the API callers want a best-effort attempt that can never turn a
successful container start into a failed HTTP response.
"""

import logging

from backend.lxd import client as lxd_client
from backend.tsdb import store as tsdb

log = logging.getLogger(__name__)


def sample_container(container: dict, resolution: str = "raw") -> dict:
    """Read live state for one container and persist it as a metric point.

    Args:
        container: A container row (needs "id" and "lxd_name").
        resolution: TinyFlux resolution tag. Always "raw" for live samples.

    Returns:
        The flat metrics dict that was written.

    Raises:
        LXDUnavailableError: if LXD is unreachable or the container is not in
            a state where metrics are readable — a stopped container included.
    """
    state = lxd_client.get_container_state(container["lxd_name"])
    tsdb.write_point(
        container_id=container["id"],
        fields=state,
        resolution=resolution,
    )
    return state


def try_sample_container(container: dict, reason: str = "") -> dict | None:
    """Best-effort sample: return the metrics dict, or None on any failure.

    Used on the request path, where a sample is an optimisation rather than
    the point of the call. A container that was just created is still stopped,
    and a container that was just started may not have readable state for
    another moment — both raise from LXD, and neither is an error worth
    failing the user's action over. The next collector tick will pick it up.

    Every failure is logged at debug rather than warning for the same reason:
    the expected case is "not running yet", which is not a fault.
    """
    try:
        return sample_container(container)
    except Exception as exc:  # noqa: BLE001 — see module docstring
        log.debug(
            "Immediate sample skipped for %s%s: %s",
            container.get("lxd_name"),
            f" ({reason})" if reason else "",
            exc,
        )
        return None
