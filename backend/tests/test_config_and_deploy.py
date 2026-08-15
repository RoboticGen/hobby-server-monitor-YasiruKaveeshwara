"""
Configuration, documentation and deployment-unit invariants.

Everything checked here is a claim made in a file that no other test reads:
the collector interval the brief specifies, the schema pasted into README.md,
the relative links in the READMEs, and the sandboxing directives on the systemd
units. Each of these has already drifted from reality at least once during this
project, and every one of them drifts *silently* — a wrong number in a table or
a link into a gitignored directory breaks for a reader, not for the test suite.

These tests are deliberately written against the real artefacts (schema.sql,
the .service files, git's own ignore rules) rather than against copies, so the
failure mode of forgetting to update a document is a red test rather than a
reviewer finding the contradiction first.
"""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

from backend import config as config_module
from backend.config import load_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO_ROOT / "deploy"


def _read(rel: str) -> str:
    path = _REPO_ROOT / rel
    assert path.is_file(), f"expected {rel} to exist at {path}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# B5 — the collector interval and everything that must agree with it
# --------------------------------------------------------------------------

# Pulls `const NAME = 5_000;` out of a .tsx file, tolerating the numeric
# separator TypeScript allows. Used to read the browser's poll rates without
# duplicating them here — a copy in this file would be one more thing to drift.
_TS_CONST_RE = r"const\s+{name}\s*=\s*([\d_]+)\s*;"


def _ts_number(rel: str, name: str) -> int:
    """Read a numeric `const` out of a TypeScript source file."""
    match = re.search(_TS_CONST_RE.format(name=re.escape(name)), _read(rel))
    assert match, f"expected a `const {name} = <number>;` in {rel}"
    return int(match.group(1).replace("_", ""))


def _env_example_interval() -> int:
    """Read COLLECTOR_INTERVAL_SECONDS out of backend/.env.example."""
    match = re.search(
        r"^COLLECTOR_INTERVAL_SECONDS=(\d+)\s*$",
        _read("backend/.env.example"),
        re.MULTILINE,
    )
    assert match, "backend/.env.example must set COLLECTOR_INTERVAL_SECONDS"
    return int(match.group(1))


class TestCollectorInterval:
    """The collector's cadence, and the things that are only correct relative to it.

    These assert *coherence* rather than a specific number of seconds. The
    interval is a tuning decision the operator owns — it is an environment
    variable precisely so it can change — but three other values are only
    correct relative to whatever it is set to, and each of them is written down
    somewhere no other test reads:

    - the browser's poll rates, which return an identical payload on every tick
      that lands between two collector writes;
    - `.env.example`, which is what an operator copies on a fresh clone;
    - the READMEs, which state the cadence as prose and as retention arithmetic.

    Pinning the literal number instead would make this class fail every time
    someone legitimately retunes the interval, which trains people to edit the
    test rather than fix the drift. Pinning the relationships fails only when
    the codebase has actually become self-contradictory.
    """

    @staticmethod
    def _isolated_env(monkeypatch):
        """Make load_config() read the environment and nothing else.

        load_config() calls load_dotenv() on backend/.env, so simply deleting
        COLLECTOR_INTERVAL_SECONDS from the environment does not reach the
        default — dotenv puts the developer's own value straight back. An
        earlier version of this test did exactly that and passed even with the
        default changed underneath it, which is the failure mode these tests
        exist to prevent. backend/.env is gitignored, so on a fresh clone the
        default is the value that actually ships, and it is what must be
        asserted against.
        """
        monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: False)
        for name, value in {
            "GOOGLE_CLIENT_ID": "probe-client-id",
            "GOOGLE_CLIENT_SECRET": "probe-secret",
            "GOOGLE_REDIRECT_URI": "http://localhost:8000/api/auth/google/callback",
            "JWT_SECRET": "probe-jwt-secret-long-enough-for-hs256-usage",
            "ADMIN_BOOTSTRAP_EMAIL": "probe@example.com",
            "DATABASE_PATH": "data/app.db",
            "TINYFLUX_PATH": "data/metrics.tinyflux",
            "LXD_ENDPOINT": "unix:///var/snap/lxd/common/lxd/unix.socket",
            "FRONTEND_ORIGIN": "http://localhost:4321",
        }.items():
            monkeypatch.setenv(name, value)

    def _shipped_default(self, monkeypatch) -> int:
        """The interval a fresh clone runs at, with nothing configured."""
        self._isolated_env(monkeypatch)
        monkeypatch.delenv("COLLECTOR_INTERVAL_SECONDS", raising=False)
        return load_config().collector_interval_seconds

    def test_shipped_default_is_a_sane_interval(self, monkeypatch):
        """A default of 0 would spin, and a huge one would look broken.

        This is the vacuity guard for every test below that compares against
        the default: if the parse silently produced 0, the comparisons would
        still pass against a frontend constant of 0.
        """
        default = self._shipped_default(monkeypatch)
        assert 1 <= default <= 60, (
            f"collector default of {default}s is outside the range a live "
            "dashboard can sensibly poll against"
        )

    def test_env_var_still_overrides_the_default(self, monkeypatch):
        """The default must remain a default, not a hardcoded constant."""
        self._isolated_env(monkeypatch)
        monkeypatch.setenv("COLLECTOR_INTERVAL_SECONDS", "30")
        assert load_config().collector_interval_seconds == 30

    def test_dotenv_is_actually_neutralised_by_the_helper(self, monkeypatch):
        """Prove the isolation works, so the default test cannot go vacuous.

        If load_dotenv were still active, this would pick up whatever
        backend/.env says instead of the sentinel set here.
        """
        self._isolated_env(monkeypatch)
        monkeypatch.setenv("FRONTEND_ORIGIN", "http://sentinel.invalid")
        assert load_config().frontend_origin == "http://sentinel.invalid"

    def test_env_example_matches_the_shipped_default(self, monkeypatch):
        """.env.example is what an operator copies; it must not disagree.

        An operator who copies the example gets whatever it says, so if it
        drifts from the default the two supported ways of starting the project
        collect at different rates — and only one of them matches the docs.
        """
        assert _env_example_interval() == self._shipped_default(monkeypatch), (
            "backend/.env.example and config.py's default disagree about "
            "COLLECTOR_INTERVAL_SECONDS"
        )

    @pytest.mark.parametrize(
        ("component", "constant"),
        [
            ("frontend/src/components/ContainerResourceGraphs.tsx", "LIVE_POLL_MS"),
            ("frontend/src/components/MetricTile.tsx", "POLL_INTERVAL_MS"),
        ],
    )
    def test_browser_poll_rate_matches_the_collector(
        self, component, constant, monkeypatch
    ):
        """Polling faster than the writer returns the same payload twice.

        This is the whole point of the class. The live view's only data source
        is what the collector has already written to TinyFlux, so a tick that
        lands between two writes re-fetches a point the client already holds.
        That is not merely wasted work: a duplicate point has a zero time
        delta, and CPU% and network throughput are both rates differenced
        between two readings, so the duplicate is silently discarded and the
        rate updates at half the advertised frequency. Polling slower than the
        collector writes is the opposite fault — samples are collected, stored,
        and never shown.
        """
        poll_ms = _ts_number(component, constant)
        expected_ms = self._shipped_default(monkeypatch) * 1000
        assert poll_ms == expected_ms, (
            f"{constant} is {poll_ms}ms but the collector writes every "
            f"{expected_ms}ms; the live view would "
            + (
                "re-read points it already has"
                if poll_ms < expected_ms
                else "miss samples"
            )
        )

    def test_cold_start_polls_faster_than_the_steady_state(self):
        """The cold-start rate exists to beat the collector, not match it.

        A just-created container has no points, and a rate needs two. Polling
        the cold-start path at the steady-state rate would give up the entire
        benefit of the `fresh=1` sample, which is there to fill the graph
        before the collector's next tick rather than after it.
        """
        graphs = "frontend/src/components/ContainerResourceGraphs.tsx"
        cold = _ts_number(graphs, "COLD_START_POLL_MS")
        live = _ts_number(graphs, "LIVE_POLL_MS")
        assert 0 < cold < live, (
            f"COLD_START_POLL_MS ({cold}ms) must be faster than "
            f"LIVE_POLL_MS ({live}ms) to be worth having"
        )

    @pytest.mark.parametrize("doc", ["README.md", "backend/README.md"])
    def test_docs_state_the_shipped_interval(self, doc, monkeypatch):
        """No document may name a cadence that contradicts the code.

        Matches only phrasings that describe the *collector's* cadence, each
        with the number left open. The unrelated 5-second SQLite busy timeout
        and the footprint script's own sampling rate both legitimately use a
        number of seconds and must not be caught here.
        """
        expected = self._shipped_default(monkeypatch)
        patterns = (
            r"Polls LXD every (\d+)s",
            r"(\d+)-second samples",
            r"polling every (\d+) seconds",
            r"raw (\d+)s points",
            r"default: (\d+)s",
        )

        body = _read(doc)
        found = [
            (match.group(0), int(match.group(1)))
            for pattern in patterns
            for match in re.finditer(pattern, body)
        ]
        assert found, (
            f"{doc} no longer states the collector cadence in any recognised "
            "phrasing — either the doc dropped it or this test's patterns are "
            "stale, and a silently vacuous test is worse than either"
        )

        wrong = [phrase for phrase, seconds in found if seconds != expected]
        assert not wrong, (
            f"{doc} claims a collector interval that is not the shipped "
            f"{expected}s: {wrong}"
        )


# --------------------------------------------------------------------------
# C2 — the schema pasted into README.md must match schema.sql
# --------------------------------------------------------------------------

_COLUMN_RE = re.compile(r"^\s{2,}(\w+)\s+(TEXT|INTEGER|REAL)\b", re.MULTILINE)


def _tables(sql: str) -> dict[str, set[str]]:
    """Map table name -> set of column names, parsed from CREATE TABLE blocks."""
    tables: dict[str, set[str]] = {}
    for match in re.finditer(r"CREATE TABLE (\w+)\s*\((.*?)\n\);", sql, re.DOTALL):
        name, body = match.group(1), match.group(2)
        tables[name] = {m.group(1) for m in _COLUMN_RE.finditer(body)}
    return tables


class TestReadmeSchemaMatchesSchemaSql:
    """README.md pastes the SQL schema; a paste goes stale in silence."""

    def test_parser_finds_the_real_tables(self):
        """Guard against the regex silently matching nothing."""
        real = _tables(_read("backend/db/schema.sql"))
        assert set(real) == {
            "users",
            "sessions",
            "containers",
            "assignments",
            "audit_log",
        }, f"parsed {sorted(real)}"
        # A column count sanity floor, so a body-regex change cannot make
        # every table parse as empty and pass the comparison vacuously.
        assert len(real["containers"]) == 9, sorted(real["containers"])

    def test_every_table_and_column_appears_in_the_readme(self):
        """The README's SQL block must cover the real schema exactly.

        This is the test that would have caught C2: `containers` gained
        limit_ram_mb, limit_cpu and limit_disk_gb in schema.sql, and the
        README's copy was never updated.
        """
        real = _tables(_read("backend/db/schema.sql"))
        documented = _tables(_read("README.md"))

        assert set(documented) == set(real), (
            f"README documents tables {sorted(documented)}, "
            f"schema.sql defines {sorted(real)}"
        )

        for table, columns in real.items():
            missing = columns - documented[table]
            extra = documented[table] - columns
            assert not missing, f"README omits {table} columns: {sorted(missing)}"
            assert not extra, f"README invents {table} columns: {sorted(extra)}"


# --------------------------------------------------------------------------
# C1 — relative links must resolve for someone who clones the repo
# --------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")

# Fenced blocks and inline code spans are stripped before links are extracted.
# Without this, a regex written inside backticks — backend/README.md documents
# the container-name pattern `^[a-z][a-z0-9-]{0,61}[a-z0-9]$` — parses as a
# markdown link and reports a nonexistent file.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_CODESPAN_RE = re.compile(r"`[^`\n]*`")


def _prose_only(markdown: str) -> str:
    return _CODESPAN_RE.sub("", _FENCE_RE.sub("", markdown))


def _git_ignores(path: Path) -> bool:
    """True if git would ignore this path (so a cloner would not receive it)."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(path)],
        cwd=_REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


class TestRelativeDocLinksResolve:
    """A link into a gitignored directory is broken for everyone but the author."""

    @pytest.mark.parametrize(
        "doc",
        ["README.md", "REPORT.md", "backend/README.md", "frontend/README.md"],
    )
    def test_relative_links_point_at_committed_files(self, doc):
        base = (_REPO_ROOT / doc).parent
        broken: list[str] = []
        ignored: list[str] = []

        for target in _LINK_RE.findall(_prose_only(_read(doc))):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith(
                ("/", "mailto:")
            ):
                continue  # external URL or absolute path — not our business
            # Percent-decode: Astro's dynamic-route filenames contain brackets,
            # which are escaped as %5B/%5D inside markdown link targets.
            resolved = (base / unquote(target)).resolve()
            if not resolved.exists():
                broken.append(target)
            elif _git_ignores(resolved):
                ignored.append(target)

        assert not broken, f"{doc} links to non-existent paths: {broken}"
        # This is the C1 assertion: docs/ is gitignored, so a link into it
        # resolves on the author's machine and 404s for every cloner.
        assert (
            not ignored
        ), f"{doc} links to gitignored paths (broken after clone): {ignored}"

    def test_the_link_checker_actually_finds_links(self):
        """Guard against the regex matching nothing and passing vacuously."""
        found = _LINK_RE.findall(_prose_only(_read("README.md")))
        assert len(found) >= 4, f"only found {len(found)} links in README.md"
        # The in-repo pointer that replaced the gitignored docs/ link must be
        # among them — otherwise C1 could regress to "no link at all" and this
        # class would still pass.
        assert any(t == "backend/README.md" for t in found), found


# --------------------------------------------------------------------------
# C3 — systemd sandboxing
# --------------------------------------------------------------------------

_UNITS = ["hsm-api.service", "hsm-collector.service"]

# Directives that must be present on both units. Each one closes a specific
# escalation path available to a process whose user is in the lxd group.
_REQUIRED_HARDENING = {
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "RestartSec": "5",
}


def _directives(unit_text: str) -> dict[str, list[str]]:
    """Parse Key=Value lines, ignoring comments. Values collect into a list."""
    out: dict[str, list[str]] = {}
    for line in unit_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out.setdefault(key.strip(), []).append(value.strip())
    return out


class TestSystemdHardening:
    """The units run as a user in the lxd group, which is root-equivalent."""

    @pytest.mark.parametrize("unit", _UNITS)
    @pytest.mark.parametrize("key,expected", sorted(_REQUIRED_HARDENING.items()))
    def test_hardening_directive_is_set(self, unit, key, expected):
        found = _directives(_read(f"deploy/{unit}")).get(key)
        assert found == [expected], f"{unit} must set {key}={expected}, found {found}"

    @pytest.mark.parametrize("unit", _UNITS)
    def test_still_runs_as_the_unprivileged_account(self, unit):
        """Hardening must not have disturbed the service account."""
        assert _directives(_read(f"deploy/{unit}")).get("User") == ["hsm-runner"]

    @pytest.mark.parametrize("unit", _UNITS)
    def test_writable_paths_are_restored_under_protectsystem_strict(self, unit):
        """ProtectSystem=strict mounts everything read-only.

        Without ReadWritePaths for the data directory, SQLite fails with
        "attempt to write a readonly database" and TinyFlux with EROFS —
        and neither is fixable by chown, because the mount is what refuses.
        The LXD socket needs it too: connecting to an AF_UNIX socket is a
        write on the socket inode.
        """
        rw = _directives(_read(f"deploy/{unit}")).get("ReadWritePaths", [])
        joined = " ".join(rw)
        assert rw, f"{unit} sets ProtectSystem=strict but no ReadWritePaths"
        assert (
            "/data" in joined
        ), f"{unit} must keep the data directory writable, got {rw}"
        assert (
            "lxd" in joined
        ), f"{unit} must keep the LXD socket path writable, got {rw}"

    @pytest.mark.parametrize("unit", _UNITS)
    def test_unit_still_has_an_execstart_and_install_section(self, unit):
        body = _read(f"deploy/{unit}")
        assert _directives(body).get("ExecStart"), f"{unit} lost its ExecStart"
        assert "[Install]" in body, f"{unit} lost its [Install] section"
