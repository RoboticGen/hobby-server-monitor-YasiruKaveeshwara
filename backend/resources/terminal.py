"""
Terminal exec endpoint.

Runs a single command inside a container via pylxd's exec and returns its
stdout/stderr/exit code — a real exec, not a simulation, with no persistent
shell session.

Lives in its own module rather than in containers.py because it has a
different authorization model: container CRUD is admin-only, but exec is
available to any user with an active assignment to the target container.
Keeping them separate stops the two access patterns from being confused.
"""

import falcon

from backend.auth.middleware import require_container_access
from backend.db import repo
from backend.lxd import client as lxd_client


class ContainerExecResource:
    """POST /api/containers/{id}/exec — run one command inside a container.

    Reads the command as a JSON array of strings and passes it straight to
    pylxd's exec as an argument list. Access is re-checked on every call
    (never cached) so a mid-session revocation takes effect immediately.
    Every invocation is written to the audit log.
    """

    def on_post(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        container_id: str,
    ) -> None:
        """Execute a command inside the given container and return its output."""
        # Re-check access on every single call, never cached. If an admin
        # revokes this user's assignment mid-session, the very next exec
        # must be rejected rather than served from a stale prior check.
        # Raises 401 if unauthenticated, 403 if the user lacks access.
        require_container_access(req, container_id)

        body = req.get_media()
        command = body.get("command")

        # --- Injection defense (decision 7.7) ---
        # The command MUST arrive as an array of strings and is handed to
        # pylxd's exec as an argument list — it is never joined into a shell
        # string, so there is no shell to inject into. Rejecting a plain
        # string here is the actual security boundary, not mere input
        # validation for tidiness: a string like "echo hi; rm -rf /" would,
        # if it ever reached a shell, run two commands; as a rejected
        # non-array it runs none. Each element must itself be a string so a
        # nested list/dict can't smuggle structure past this check.
        if not isinstance(command, list) or not all(
            isinstance(arg, str) for arg in command
        ):
            raise falcon.HTTPBadRequest(
                title="Invalid command",
                description="The 'command' field must be a JSON array of "
                'strings, e.g. {"command": ["echo", "hello"]}. A single '
                "string is rejected because it would reintroduce a "
                "shell-injection surface.",
            )
        if not command:
            raise falcon.HTTPBadRequest(
                title="Empty command",
                description="The 'command' array must contain at least one "
                "element (the program to run).",
            )

        # Look up the container's CURRENT lxd_name at call time — it may have
        # been renamed since the frontend last loaded, and the audit trail
        # must reference the name the command actually ran against.
        container = repo.get_container_by_id(container_id)
        if container is None or container["deleted_at"] is not None:
            raise falcon.HTTPNotFound(
                title="Container not found",
                description=f"No active container with id '{container_id}'.",
            )
        lxd_name = container["lxd_name"]

        # Run the command as an argument array (decision 7.7's safe path).
        try:
            exit_code, stdout, stderr = lxd_client.execute_command(
                lxd_name, command
            )
        except Exception as exc:
            # A failed exec (e.g. container not running, LXD unreachable) is
            # surfaced as a clean error rather than a raw stack trace.
            raise falcon.HTTPServiceUnavailable(
                title="Command execution failed",
                description=f"Could not run the command: {exc}",
            )

        # Terminal actions are logged (decision: destructive/interactive
        # actions leave a trail). The exact argv is recorded so an admin can
        # see precisely what was run — joined with spaces for readability
        # only; the stored detail is a record, never re-executed.
        repo.write_audit_log(
            user_id=req.context.user["id"],
            action="container.exec",
            target=container_id,
            detail=f"Ran command in '{lxd_name}': {' '.join(command)}",
        )

        resp.media = {
            "container_id": container_id,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }
