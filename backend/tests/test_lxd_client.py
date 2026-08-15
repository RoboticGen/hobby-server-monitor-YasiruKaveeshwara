"""
Tests for the LXD client wrapper.

These cover endpoint normalization so `unix://`-style LXD socket URIs
are accepted by pylxd and do not trigger a `requests.InvalidSchema` error,
and the unprivileged-container invariant of decision 7.7.
"""

from unittest.mock import MagicMock, patch

from backend.lxd import client as lxd_client
from backend.lxd.client import _normalize_lxd_endpoint


def test_normalize_lxd_endpoint_rewrites_unix_uri_to_path() -> None:
    """A unix:// URI should be rewritten to a bare path for pylxd."""
    assert (
        _normalize_lxd_endpoint("unix:///var/snap/lxd/common/lxd/unix.socket")
        == "/var/snap/lxd/common/lxd/unix.socket"
    )
    assert (
        _normalize_lxd_endpoint("unix://var/snap/lxd/common/lxd/unix.socket")
        == "/var/snap/lxd/common/lxd/unix.socket"
    )


def test_normalize_lxd_endpoint_leaves_http_endpoint_unchanged() -> None:
    """HTTP(S) endpoints are not modified by the normalization helper."""
    assert _normalize_lxd_endpoint("https://example.com") == "https://example.com"


def test_normalize_lxd_endpoint_leaves_path_unchanged() -> None:
    """A raw unix socket path should pass through unchanged."""
    assert (
        _normalize_lxd_endpoint("/var/snap/lxd/common/lxd/unix.socket")
        == "/var/snap/lxd/common/lxd/unix.socket"
    )


def _capture_create(limits: dict) -> dict:
    """Call create_container with `limits` and return the config sent to LXD."""
    captured: dict = {}
    fake = MagicMock()

    def _create(config, wait=True):
        captured.update(config)
        return MagicMock(
            name="c",
            status="Stopped",
            architecture="x86_64",
            created_at="2026-01-01",
            config={},
        )

    fake.containers.create.side_effect = _create
    with patch.object(lxd_client, "_get_client", return_value=fake):
        lxd_client.create_container("c1", "ubuntu:22.04", limits)
    return captured["config"]


def test_create_container_forces_unprivileged() -> None:
    """Decision 7.7: every container is created with privilege disabled."""
    config = _capture_create({"limits.memory": "512MB"})
    assert config["security.privileged"] == "false"


def test_create_container_overrides_caller_supplied_privileged() -> None:
    """A caller cannot opt into a privileged container.

    A privileged container's root maps to the host's root, so a breakout is a
    host compromise. The key is applied after the caller's limits precisely so
    that a caller passing "true" -- through a future bug or a merged dict --
    cannot win. This test fails if that ordering is ever reversed.
    """
    config = _capture_create({"limits.memory": "512MB", "security.privileged": "true"})
    assert config["security.privileged"] == "false"


def test_create_container_does_not_mutate_caller_limits() -> None:
    """The forced key must not leak back into the caller's dict.

    Callers build `limits` and may reuse or persist it; silently adding a key
    to their dict would make the caller's own state depend on ours.
    """
    limits = {"limits.cpu": "2"}
    _capture_create(limits)
    assert limits == {"limits.cpu": "2"}
