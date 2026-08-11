"""
Request body validation helpers.

Falcon hands `req.get_media()` back as whatever the client's JSON decoded
to. It does not coerce types and it does not check them, so a handler that
calls `body.get("name").strip()` is trusting the client to have sent a
string. A JSON null or a number there raises AttributeError, which Falcon
renders as a 500 — telling an honest client the server is broken when its
request was simply malformed, and filling the log with tracebacks for
routine bad input.

These helpers convert that class of mistake into the 400 it always was.
They are deliberately small and explicit rather than a schema library: the
API has four bodies to validate, and a dependency would be more moving
parts than the problem needs.
"""

import falcon


def require_object(body, what: str = "Request body") -> dict:
    """Return the body as a dict, or raise HTTPBadRequest.

    A JSON array, string, or number at the top level decodes fine and then
    fails on the first `.get()`. Checking once here means every later field
    read can assume a mapping.
    """
    if not isinstance(body, dict):
        raise falcon.HTTPBadRequest(
            title=f"Invalid {what.lower()}",
            description=f"{what} must be a JSON object.",
        )
    return body


def validate_string(value, field: str, *, required: bool = True) -> str:
    """Return a stripped string, or raise HTTPBadRequest.

    `bool` is rejected even though it is not a str, because JSON `true`
    reaching a name field is a client bug worth reporting rather than
    something to coerce.
    """
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise falcon.HTTPBadRequest(
            title=f"Invalid {field}",
            description=f"'{field}' must be a string.",
        )
    return value.strip()


def validate_non_negative_number(value, field: str, kind):
    """Return value coerced to `kind` (int or float), rejecting bad input.

    Two failure modes, both 400:

    - Not a number at all ("lots", null, a list). `int()` would raise
      ValueError or TypeError here and surface as a 500.
    - Negative. A negative limit used to pass the cast and then get dropped
      by the `if ram_mb > 0` guard further down, so the container was
      created with no limit at all while the response echoed the negative
      number back as if it had been applied. Rejecting here means the
      caller is never told a limit was set that was not.

    `bool` is excluded deliberately: `isinstance(True, int)` is True in
    Python, so without the check `{"ram_mb": true}` would silently become
    1MB.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise falcon.HTTPBadRequest(
            title=f"Invalid {field}",
            description=f"'{field}' must be a number.",
        )
    if value < 0:
        raise falcon.HTTPBadRequest(
            title=f"Invalid {field}",
            description=f"'{field}' must not be negative (got {value}).",
        )
    return kind(value)


def validate_limits(limits_body, defaults: dict) -> tuple[int, float, int]:
    """Validate a `limits` sub-object, returning (ram_mb, cpu, disk_gb).

    `defaults` supplies the value for any key the client omitted — the
    create path defaults to 0 (meaning unlimited), while the update path
    defaults to the container's current limits so a partial body only
    changes what it names.

    Raises HTTPBadRequest if `limits` is not an object, or if any present
    value is not a non-negative number.
    """
    if not isinstance(limits_body, dict):
        raise falcon.HTTPBadRequest(
            title="Invalid limits",
            description="'limits' must be a JSON object with ram_mb, cpu, "
            "and disk_gb keys.",
        )

    ram_mb = validate_non_negative_number(
        limits_body.get("ram_mb", defaults["ram_mb"]), "ram_mb", int
    )
    cpu = validate_non_negative_number(
        limits_body.get("cpu", defaults["cpu"]), "cpu", float
    )
    disk_gb = validate_non_negative_number(
        limits_body.get("disk_gb", defaults["disk_gb"]), "disk_gb", int
    )
    return ram_mb, cpu, disk_gb
