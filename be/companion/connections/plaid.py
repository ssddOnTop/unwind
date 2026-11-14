"""Bounded Sandbox-only provider transport. Never returns credentials to HTTP clients."""
import os
import json
from decimal import Decimal
from dataclasses import dataclass, field

import httpx


class StalePlaidOperation(Exception):
    def __init__(self, removal_confirmed=True):
        self.removal_confirmed = removal_confirmed
        super().__init__("Sandbox connection context changed.")


class PlaidFailure(Exception):
    def __init__(self, code="unavailable"):
        self.code = code if code in {"unavailable", "rate_limited", "invalid_token", "mutation", "not_ready", "auth", "invalid_verification_key"} else "unavailable"
        super().__init__("Plaid Sandbox request did not complete.")


@dataclass(repr=False)
class SandboxCredentials:
    client_id: str = field(repr=False)
    secret: str = field(repr=False)

    @classmethod
    def from_environment(cls):
        client = os.environ.get("PLAID_CLIENT_ID", "").strip()
        secret = (os.environ.get("PLAID_SANDBOX_SECRET") or os.environ.get("PLAID_SECRET", "")).strip()
        return cls(client, secret) if client and secret else None


class PlaidSandboxClient:
    BASE_URL = "https://sandbox.plaid.com"
    ALLOWED = {"/link/token/create", "/item/public_token/exchange", "/transactions/sync", "/item/remove",
               "/sandbox/public_token/create", "/sandbox/transactions/create", "/sandbox/item/reset_login",
               "/webhook_verification_key/get", "/accounts/get"}

    def __init__(self, credentials: SandboxCredentials | None, transport=None):
        self.credentials = credentials
        self.transport = transport

    @property
    def configured(self):
        return self.credentials is not None

    async def request(self, endpoint: str, payload: dict) -> dict:
        if endpoint not in self.ALLOWED or self.credentials is None:
            raise PlaidFailure()
        # Fixed origin; no ambient proxy, redirects, caller headers, URLs, or logging.
        try:
            async with httpx.AsyncClient(base_url=self.BASE_URL, transport=self.transport, timeout=12,
                                         follow_redirects=False, trust_env=False) as client:
                body = {**payload, "client_id": self.credentials.client_id, "secret": self.credentials.secret}
                if endpoint == '/webhook_verification_key/get':
                    async with client.stream('POST', endpoint, json=body) as response:
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 8192:
                                raise PlaidFailure()
                            chunks.append(chunk)
                        raw = b''.join(chunks)
                else:
                    response = await client.post(endpoint, json=body)
                    raw = response.content
                def unique(pairs):
                    result = {}
                    for k, v in pairs:
                        if k in result: raise ValueError('Duplicate JSON property')
                        result[k] = v
                    return result
                data = json.loads(raw, parse_float=Decimal, object_pairs_hook=unique,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (httpx.HTTPError, ValueError):
            raise PlaidFailure() from None
        if response.status_code == 401:
            self.credentials = None
            raise PlaidFailure("auth")
        if response.status_code != 200 or not isinstance(data, dict) or data.get("error_code"):
            code = data.get("error_code", "") if isinstance(data, dict) else ""
            safe = {"TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION": "mutation", "PRODUCT_NOT_READY": "not_ready",
                    "RATE_LIMIT_EXCEEDED": "rate_limited", "INVALID_PUBLIC_TOKEN": "invalid_token",
                    "INVALID_ACCESS_TOKEN": "invalid_token", "ITEM_LOGIN_REQUIRED": "invalid_token",
                    "INVALID_WEBHOOK_VERIFICATION_KEY_ID": "invalid_verification_key"}.get(code, "unavailable")
            raise PlaidFailure(safe)
        return data

    async def sync_pages(self, access_token: str, cursor: str | None, guard=lambda: None, *, account_id=None) -> dict:
        # Restart the entire bounded pagination from the original durable cursor.
        pages_used = 0
        for restart in range(3):
            working = cursor
            staged = {"added": [], "modified": [], "removed": []}
            try:
                for page_number in range(20):
                    guard()
                    if pages_used >= 20:
                        raise PlaidFailure()
                    pages_used += 1
                    body = {"access_token": access_token, "count": 500}
                    if account_id is not None:
                        body['options'] = {'account_id': account_id}
                    if working:
                        body["cursor"] = working
                    data = await self.request("/transactions/sync", body)
                    guard()
                    for field_name in staged:
                        values = data.get(field_name)
                        if not isinstance(values, list):
                            raise PlaidFailure()
                        staged[field_name].extend(values)
                    next_cursor = data.get("next_cursor")
                    if not isinstance(next_cursor, str) or not isinstance(data.get("has_more"), bool):
                        raise PlaidFailure()
                    working = next_cursor
                    if not data["has_more"]:
                        staged.update(cursor=working, status=data.get("transactions_update_status", "UNKNOWN"))
                        return staged
                    if page_number == 19:
                        raise PlaidFailure()
            except PlaidFailure as exc:
                if exc.code != "mutation" or restart == 2:
                    raise
        raise PlaidFailure()
