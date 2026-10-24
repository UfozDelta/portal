"""Error types. Every message here is printed straight to the user, so they must
be actionable and must never contain a credential."""


class PortalError(Exception):
    """Base for anything portal raises deliberately. Callers of api.py (or the
    __main__ shim) catch this and print the message without a traceback."""


class ConfigError(PortalError):
    """Missing/invalid local configuration or credentials."""


class DetectionError(PortalError):
    """Could not work out what to deploy from the project directory."""


class DockerError(PortalError):
    """A docker/docker compose invocation failed."""


class CloudflareError(PortalError):
    """The Cloudflare API rejected a request or returned something unusable.

    `status_code` is the HTTP status when there was a response at all, so
    callers can tell "that thing is already gone" (404) apart from a real
    failure without string-matching the message."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OwnershipError(CloudflareError):
    """A DNS record exists but is not stamped as portal's for this slug. This
    is a deliberate, permanent refusal — retrying will never change it."""


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
