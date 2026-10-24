"""Minimal Cloudflare API v4 client.

Only exercised by the live test suite (`pytest -m live`). The surface is small,
and the interesting logic (ingress rule building, zone matching) lives in pure
functions that are unit-tested.
"""

from __future__ import annotations

import time

import requests

from ..errors import CloudflareError, redact

BASE_URL = "https://api.cloudflare.com/client/v4"
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Only retry a bad status on methods where re-sending is safe. A POST that got
# a 5xx may have already been applied server-side (a tunnel or DNS record
# actually created) before the response was lost — retrying it risks creating
# a duplicate. GET/PUT/DELETE are idempotent, so re-sending is always safe.
IDEMPOTENT_METHODS = {"GET", "PUT", "DELETE"}
MAX_ATTEMPTS = 4


class CloudflareClient:
    def __init__(self, token: str, *, base_url: str = BASE_URL, timeout: int = 30):
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def _scrub(self, text: str) -> str:
        """Keep the API token out of anything we raise or print."""
        return redact(text, self._token)

    def request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_error = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_error = self._scrub(str(exc))
                if attempt == MAX_ATTEMPTS:
                    raise CloudflareError(
                        f"Could not reach the Cloudflare API: {last_error}"
                    ) from None
                time.sleep(2 ** (attempt - 1))
                continue

            if (
                resp.status_code in RETRY_STATUSES
                and method in IDEMPOTENT_METHODS
                and attempt < MAX_ATTEMPTS
            ):
                time.sleep(2 ** (attempt - 1))
                continue

            return self._unwrap(resp, method, path)

        raise CloudflareError(f"Cloudflare API gave up after retries: {last_error}")

    def _unwrap(self, resp: requests.Response, method: str, path: str) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise CloudflareError(
                f"{method} {path} returned HTTP {resp.status_code} with a "
                f"non-JSON body: {self._scrub(resp.text[:300])}",
                status_code=resp.status_code,
            ) from None

        if not body.get("success", False):
            errors = body.get("errors") or []
            messages = "; ".join(
                f"{e.get('code', '?')}: {e.get('message', '')}" for e in errors
            ) or f"HTTP {resp.status_code}"
            raise CloudflareError(
                f"Cloudflare rejected {method} {path}: {self._scrub(messages)}",
                status_code=resp.status_code,
                codes=tuple(e["code"] for e in errors if isinstance(e.get("code"), int)),
            )
        return body

    def get(self, path: str, **kwargs) -> dict:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json: dict | None = None) -> dict:
        return self.request("POST", path, json=json)

    def put(self, path: str, json: dict | None = None) -> dict:
        return self.request("PUT", path, json=json)

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)

    def verify_token(self, account_id: str) -> dict:
        """The token's own record: `status` ("active", "disabled", "expired"),
        `expires_on` if it has one, and `kind` ("account" or "user").

        There are two kinds of API token and each has its own verify endpoint.
        Each endpoint answers the other kind with the same "1000: Invalid API
        Token" a genuinely bad token gets — a known Cloudflare false negative.
        So try the account-scoped endpoint, fall back to the user one on 1000,
        and only a token both reject is really invalid. Neither endpoint needs
        any permission beyond the token itself.
        """
        try:
            result = self.get(f"/accounts/{account_id}/tokens/verify")["result"]
            kind = "account"
        except CloudflareError as exc:
            if 1000 not in exc.codes:
                raise
            result = self.get("/user/tokens/verify")["result"]
            kind = "user"
        return {**result, "kind": kind}

    def resolve_account_id(self) -> str:
        accounts = self.get("/accounts", params={"per_page": 50})["result"]
        if not accounts:
            raise CloudflareError(
                "The token is valid but can see no accounts. It needs "
                "Account:Cloudflare Tunnel:Edit and Zone:DNS:Edit."
            )
        if len(accounts) > 1:
            names = ", ".join(f"{a['name']} ({a['id']})" for a in accounts)
            raise CloudflareError(
                "The token can see several accounts; portal cannot guess which "
                f"to use. Set CLOUDFLARE_ACCOUNT_ID to one of: {names}"
            )
        return accounts[0]["id"]
