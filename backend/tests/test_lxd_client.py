"""
Tests for the LXD client wrapper.

These cover endpoint normalization so `unix://`-style LXD socket URIs
are accepted by pylxd and do not trigger a `requests.InvalidSchema` error.
"""

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
