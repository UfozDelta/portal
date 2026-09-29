"""Error types. Every message here is printed straight to the user, so they must
be actionable and must never contain a credential."""


class PortalError(Exception):
    """Base for anything portal raises deliberately. Callers of api.py (or the
    __main__ shim) catch this and print the message without a traceback."""

    exit_code = 1


class ConfigError(PortalError):
    """Missing/invalid local configuration or credentials."""


class DetectionError(PortalError):
    """Could not work out what to deploy from the project directory."""


class DockerError(PortalError):
    """A docker/docker compose invocation failed."""


class CloudflareError(PortalError):
    """The Cloudflare API rejected a request or returned something unusable."""


class StateError(PortalError):
    """state.json is missing an entry, or is unreadable."""


def redact(text: str, *secrets: str) -> str:
    """Strip known secret values out of a string before it reaches a log or an
    exception message. Short/empty secrets are ignored so we don't mangle text."""
    out = text
    for s in secrets:
        if s and len(s) >= 8:
            out = out.replace(s, "***REDACTED***")
    return out
