"""Replaceable Plaid Sandbox capability: fixed-origin transport and encrypted private state."""
import asyncio
from copy import deepcopy
from uuid import uuid4

from ..connections.plaid import PlaidFailure, PlaidSandboxClient, StalePlaidOperation
from ..contracts import DomainError
from ..domain import active, instant
from ..plaid_projection import project
from ..vault import PrivateVault


class PlaidExtension:
    id, version = "plaid", "1"

    def __init__(self, data_dir, client: PlaidSandboxClient, *, webhook_url=None, client_user_id=None, ownership_check=None):
        from urllib.parse import urlsplit
        if webhook_url:
            url = urlsplit(webhook_url)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError('Webhook URL must be operator-configured HTTPS.')
        self.webhook_url, self.client_user_id = webhook_url, client_user_id
        self.ownership_check = ownership_check
        self.client, self.vault = client, PrivateVault(data_dir)
        self.generation = 0

    @property
    def status(self):
        return "available" if self.client.configured else "not_configured"

    def check(self, generation):
        if generation != self.generation:
            raise StalePlaidOperation()

    async def remove(self, token, item_id=None):
        if not token:
            return True
        try:
            if self.ownership_check is not None and not await self.ownership_check(item_id):
                return False
            await asyncio.wait_for(self.client.request("/item/remove", {"access_token": token}), timeout=5)
            return True
        except Exception:
            return False

    async def handle(self, operation: str, payload: dict) -> dict:
        if operation == "invalidate":
            self.generation += 1
            return {"invalidated": True}
        if operation == "digest":
            return {"digest": self.vault.digest(payload)}
        if operation == "prune":
            connection = self.vault.open(payload.get("vault"))
            if connection:
                allowed = set(payload["observation_ids"])
                connection["records"] = {k: v for k, v in connection.get("records", {}).items() if v in allowed}
            return {"vault": self.vault.seal(connection) if connection else None}
        if operation == "recover":
            return {"recoverable": bool(self.vault.pending())}
        if operation == "receipt_seal":
            return {"ciphertext": self.vault.seal(payload)}
        if operation == "receipt_open":
            return self.vault.open(payload["ciphertext"])
        if operation == "committed":
            self.vault.clear_pending()
            return {"committed": True}
        if operation in {"disconnect", "erase"}:
            self.generation += 1
            connection = self.vault.open(payload.get("vault"))
            pending = self.vault.pending()
            token = connection.get("access_token") or pending.get("connection", {}).get("access_token")
            item_id = connection.get('item_id') or pending.get('connection', {}).get('item_id')
            if operation == "erase":
                self.vault.clear()
            else:
                self.vault.clear_pending()
            confirmed = await self.remove(token, item_id)
            revocation = {"access_token": token, 'item_id': item_id, "removal_pending": True} if token and not confirmed and operation != "erase" else {}
            return {"confirmed": confirmed, "vault": self.vault.seal(revocation) if revocation else None,
                    "status": "disconnected" if confirmed else "removal_pending"}
        if self.status != "available":
            raise DomainError("not_configured", 409)
        if operation == "validate_observation":
            # Public observation uploads never call this trusted server-import path.
            raise DomainError("consent_required", 409)
        generation = self.generation
        connection = self.vault.open(payload.get("vault"))
        if operation == "link_token":
            if connection.get("access_token"):
                raise DomainError("busy", 409)
            body = {"client_name": "Local Companion", "language": "en", "country_codes": ["US"],
                "products": ["transactions"], "transactions": {"days_requested": 90},
                "user": {"client_user_id": self.client_user_id or str(uuid4())}}
            if self.webhook_url:
                body['webhook'] = self.webhook_url
            data = await self.client.request("/link/token/create", body)
            self.check(generation)
            if not isinstance(data.get("link_token"), str) or not data["link_token"].startswith("link-sandbox-"):
                raise PlaidFailure()
            return {"result": {"link_token": data["link_token"], "expiration": data.get("expiration")},
                    "vault": self.vault.seal({"reservation": True, "epoch": payload["epoch"], "expiration": data.get("expiration")})}
        if operation == "exchange":
            if not connection.get("reservation") or connection.get("epoch") != payload["epoch"]:
                raise DomainError("consent_required", 409)
            if connection.get("expiration") and instant(payload["now"]) >= instant(connection["expiration"]):
                raise DomainError("consent_required", 409, "Sandbox Link reservation expired; start a new session.")
            pending = self.vault.pending()
            if pending.get("digest") == payload["digest"] and pending.get("epoch") == payload["epoch"]:
                connected = pending["connection"]
            else:
                data = await self.client.request("/item/public_token/exchange", {"public_token": payload["public_token"]})
                token = data.get("access_token")
                if not isinstance(token, str) or not token.startswith("access-sandbox-"):
                    raise PlaidFailure()
                if generation != self.generation:
                    confirmed = await self.remove(token, data.get('item_id'))
                    raise StalePlaidOperation(removal_confirmed=confirmed)
                connected = {"access_token": token, "item_id": data.get("item_id"), "epoch": payload["epoch"], "records": {}, "cursor": None}
                self.vault.stage_exchange({"digest": payload["digest"], "epoch": payload["epoch"], "connection": connected})
            self.check(generation)
            item_id = connected.get('item_id')
            if not isinstance(item_id, str) or not 1 <= len(item_id) <= 256:
                raise PlaidFailure()
            return {"result": {"status": "connected"}, "vault": self.vault.seal(connected), 'item_id': item_id}
        if operation == "sync":
            if not connection.get("access_token") or connection.get("removal_pending"):
                raise DomainError("not_found", 404)
            staged = await asyncio.wait_for(self.client.sync_pages(connection["access_token"], connection.get("cursor"),
                                                                     guard=lambda: self.check(generation)), timeout=25)
            self.check(generation)
            projected = project(payload["state"], connection, staged)
            rows = [o for o in active(projected["observations"]) if o["source_id"] == "plaid"]
            status = {"NOT_READY": "waiting", "INITIAL_UPDATE_COMPLETE": "partial", "HISTORICAL_UPDATE_COMPLETE": "complete"}.get(staged["status"], "partial" if rows else "waiting")
            return {"vault": self.vault.seal(projected["connection"]), "observations": projected["observations"],
                    "result": {"status": status, **projected["counts"], "retry_after_seconds": None if status == "complete" else 2}}
        raise ValueError("Unsupported Plaid operation")
