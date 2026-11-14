"""Sandbox connection transaction coordinator, outside the minimal core registry."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from .connections.plaid import PlaidFailure, StalePlaidOperation
from .contracts import DomainError
from .domain import active, defaults, iso, instant, preference_update
from .plaid_contracts import validate_profile
from . import banking_schedule as schedule


class PlaidCoordinator:
    route = "/connections/plaid"

    def __init__(self, service):
        self.service = service
        self.busy = False
        self.generation = 0
        self.task = None

    async def call(self, operation, payload):
        return await self.service.registry.dispatch("plaid", operation, payload)

    def source(self, doc):
        return next(s for s in doc["state"]["sources"] if s["id"] == "plaid")

    def initialize(self, doc):
        source = self.source(doc)
        metadata = next(m for m in self.service.registry.metadata() if m["id"] == "plaid")
        source["capability"] = metadata["status"]
        if source["consent"] == "not_requested":
            source["status"] = "disabled" if metadata["status"] == "available" else "not_configured"
            source["reason"] = "Plaid Sandbox synthetic data only; not connected." if metadata["status"] == "available" else "Plaid Sandbox credentials are not configured."

    async def recover(self, doc):
        self.initialize(doc)
        inflight = doc["meta"].pop("plaid_inflight", None)
        if inflight == "exchange" and not (await self.call("recover", {}))["recoverable"]:
            doc["meta"].pop("plaid_vault", None)
            doc["meta"]["plaid_connected"] = False
            self.source(doc).update(status="error", reason="Exchange interrupted; start a new Sandbox Link session.")

    async def invalidate(self):
        self.generation += 1
        await self.call("invalidate", {})

    async def publish(self, doc, receipt=None, response=None, erase=False):
        doc["state"]["revision"] += 1
        doc["state"]["generated_at"] = iso(self.service.clock())
        if response is not None:
            response["state"] = deepcopy(doc["state"])
            response["data_epoch"] = doc["state"]["data_epoch"]
            validate_profile("response", response)
            encrypted = await self.call("receipt_seal", response)
            receipt["response"] = {"plaid_receipt": encrypted["ciphertext"]}
            receipt["preserve_response"] = True
        await self.service.commit(doc, receipt, erase=erase, bump=False)
        return response

    async def prune_private(self, doc):
        sealed = await self.call("prune", {"vault": doc["meta"].get("plaid_vault"),
                                          "observation_ids": [o["id"] for o in doc["state"]["observations"] if o["source_id"] == "plaid"]})
        doc["meta"]["plaid_vault"] = sealed["vault"]

    async def daily_sync(self):
        async with self.service.lock:
            doc = self.service.document
            source = self.source(doc)
            if self.busy or not doc["meta"].get("plaid_connected") or not source["enabled"] or source["consent"] != "granted":
                return
            body = {"schema_version": "1", "request_id": "plaid-daily:" + str(uuid4()),
                    "data_epoch": doc["state"]["data_epoch"], "action": "sync"}
        try:
            return await self.request(body,automatic=True)
        except DomainError:
            return None

    async def before_preferences(self, body):
        async with self.service.lock:
            receipt = self.service.receipt("/preferences", body)
            if await self.service.replay(receipt) is not None:
                return
            trial = deepcopy(self.service.document["state"])
            preference_update(trial, body["preferences"], allow_plaid=bool(self.service.document["meta"].get("plaid_connected")))
            revoke = "plaid" not in body["preferences"]["enabled_source_ids"] and (self.source(self.service.document)["enabled"] or self.service.document["meta"].get("plaid_connected"))
        if revoke:
            await self.request({"schema_version": "1", "request_id": "revoke:" + str(uuid4()),
                                "data_epoch": body["data_epoch"], "action": "disconnect"})

    async def request(self, body, *, admission_guard=None, automatic=False):
        validate_profile("request", body)
        action = body["action"]
        service = self.service
        async with service.lock:
            if body["data_epoch"] != service.document["state"]["data_epoch"]:
                raise DomainError("epoch_mismatch", 409)
            # Trusted background callbacks carry a captured ownership generation.
            # Recheck inside this exact admission lock, not in a racy preflight.
            if admission_guard is not None and not await admission_guard():
                raise DomainError('consent_required', 409)
            receipt = service.receipt(self.route, body)
            receipt["digest"] = (await self.call("digest", body))["digest"]
            cached = await service.registry.dispatch("persistence", "receipt", receipt)
            if cached:
                if cached["digest"] != receipt["digest"]:
                    raise DomainError("idempotency_conflict", 409)
                if cached["response"] is None:
                    raise DomainError("busy", 409)
                return await self.call("receipt_open", {"ciphertext": cached["response"]["plaid_receipt"]})
            if self.busy and action != "disconnect":
                raise DomainError("busy", 409)
            doc = deepcopy(service.document)
            source = self.source(doc)
            connected = doc["meta"].get("plaid_connected", False)
            if action != "disconnect" and source["capability"] != "available":
                raise DomainError("not_configured", 409)
            if action == "link_token" and (connected or doc["meta"].get("plaid_removal_pending")):
                raise DomainError("busy", 409)
            if action in {"exchange", "sync"}:
                if not source["enabled"] or source["consent"] != "granted":
                    raise DomainError("consent_required", 409)
                if action == "sync" and not connected:
                    raise DomainError("not_found", 404)
                if action == "exchange" and connected:
                    raise DomainError("busy", 409)
            if action == "disconnect":
                await self.invalidate()
                source.update(enabled=False, consent="revoked", status="disabled", coverage="unknown", coverage_window=None,
                              reason="Plaid Sandbox disconnected.")
                doc["state"]["preferences"]["enabled_source_ids"] = [v for v in doc["state"]["preferences"]["enabled_source_ids"] if v != "plaid"]
                doc["meta"]["plaid_connected"] = False
                self.service.invalidate(doc["state"])
                await service.cancel_synthesis()
                await service.clear_checkpoints()
            elif action == "link_token":
                source.update(enabled=True, consent="granted", authorization_requested=True, read_access="not_applicable",
                              status="empty", reason="Awaiting Sandbox Link completion.")
                enabled = doc["state"]["preferences"]["enabled_source_ids"]
                if "plaid" not in enabled:
                    enabled.append("plaid")
                doc["meta"]["fixture_session"] = False
            sync_item_id=doc['meta'].get('plaid_item_id','legacy:'+doc['state']['data_epoch'])
            if action=='sync':
                due=schedule.next_due(doc,{'item_id':sync_item_id,'last_sync_at':source['last_success_at']},service.clock())
                if automatic and due>service.clock():return {'deferred_until':iso(due)}
                schedule.reserve(doc,sync_item_id,service.clock())
            source["last_attempt_at"] = iso(service.clock())
            doc["meta"]["plaid_inflight"] = action
            await service.evaluate(doc)
            await service.commit(doc)
            self.busy = True
            generation, epoch = self.generation, doc["state"]["data_epoch"]
            input_revision = doc["state"]["revision"]
            payload = {"vault": doc["meta"].get("plaid_vault"), "state": deepcopy(doc["state"]),
                       "epoch": epoch, "digest": receipt["digest"], "now": iso(service.clock())}
            if action == "exchange":
                payload["public_token"] = body["public_token"]
            self.task = asyncio.create_task(self.call(action, payload))
            task = self.task
        failure = None
        domain_failure = None
        late_removal_unconfirmed = False
        try:
            outcome = await task
        except (PlaidFailure, asyncio.TimeoutError) as exc:
            failure = exc.code if isinstance(exc, PlaidFailure) else "unavailable"
            outcome = {}
        except StalePlaidOperation as exc:
            failure, outcome = "stale", {}
            late_removal_unconfirmed = not exc.removal_confirmed
        except DomainError as exc:
            domain_failure = exc
            failure, outcome = "request", {}
        except Exception:
            failure, outcome = "unavailable", {}
        async with service.lock:
            if epoch != service.document["state"]["data_epoch"] or generation != self.generation:
                # The only permitted late publication is a generic cleanup warning, never data/tokens.
                if late_removal_unconfirmed:
                    warning = deepcopy(service.document)
                    if "Remote Sandbox Item removal unconfirmed." not in warning["state"]["limitations"]:
                        warning["state"]["limitations"].append("Remote Sandbox Item removal unconfirmed.")
                        await service.commit(warning)
                raise DomainError("epoch_mismatch" if epoch != service.document["state"]["data_epoch"] else "consent_required", 409)
            self.busy = False
            if action == "sync" and service.document["state"]["revision"] != input_revision:
                failure = "stale_context"
                outcome = {}
            doc = deepcopy(service.document)
            source = self.source(doc)
            doc["meta"].pop("plaid_inflight", None)
            if action=='sync':schedule.finish(doc,sync_item_id,service.clock(),not failure and outcome.get('result',{}).get('status')=='complete')
            erase = action == "disconnect"
            if failure:
                source.update(status="error", coverage="unknown", reason="Plaid Sandbox request unavailable; no partial data committed.")
                if failure == "auth":
                    source.update(capability="not_configured", status="not_configured", reason="Plaid Sandbox credentials rejected.")
                if action == "exchange":
                    doc["meta"].pop("plaid_vault", None)
                    source["reason"] = "Exchange not confirmed; start a fresh Sandbox Link session. Remote Item cleanup may be unconfirmed."
                await service.evaluate(doc)
                if action != "sync" or failure == "auth":
                    await service.commit(doc)
                    if domain_failure:
                        raise domain_failure
                    raise DomainError("not_configured" if failure == "auth" else "internal_error", 409 if failure == "auth" else 500)
                outcome["result"] = {"status": "error", "added": 0, "modified": 0, "removed": 0, "skipped": 0,
                                     "retry_after_seconds": 5 if failure not in {"invalid_token", "auth"} else None}
            else:
                doc["meta"]["plaid_vault"] = outcome.get("vault")
                if action == "exchange":
                    doc["meta"]["plaid_connected"] = True
                    doc["meta"]["plaid_item_id"] = outcome.get("item_id")
                    source.update(status="syncing", last_success_at=None, coverage="unknown", coverage_window=None,
                                  reason="Sandbox Item connected; transaction import has not completed.")
                elif action == "sync":
                    doc["state"]["observations"] = outcome["observations"]
                    rows = [o for o in active(outcome["observations"]) if o["source_id"] == "plaid"]
                    dates = sorted(o["local_date"] for o in rows)
                    source.update(status=("ready" if rows else "empty") if outcome["result"]["status"] == "complete" else "syncing",
                                  last_success_at=iso(service.clock()), coverage="partial" if rows else "unknown",
                                  coverage_window={"start_date": dates[0], "end_date": dates[-1], "timezone": doc["state"]["preferences"]["timezone"]} if dates else None,
                                  reason="Synthetic Plaid Sandbox transactions; purchases are not consumption.")
                    if outcome["result"]["skipped"]:
                        source["reason"] += " Some unsupported, old, or unchanged provider entries were skipped."
                elif action == "disconnect":
                    doc["meta"]["plaid_removal_pending"] = not outcome["confirmed"]
                    if not outcome["confirmed"]:
                        source.update(status="error", reason="Remote Sandbox Item removal unconfirmed; retry disconnect.")
                service.invalidate(doc["state"])
                await service.clear_checkpoints()
                await service.evaluate(doc)
            response = {"schema_version": "1", "request_id": body["request_id"], "data_epoch": epoch,
                        "action": action, "environment": "sandbox", "result": outcome.get("result", {"status": outcome.get("status")}), "state": {}}
            result = await self.publish(doc, receipt, response, erase=erase)
            if action == "exchange" and not failure:
                await self.call("committed", {})
            return result

    async def delete(self, body):
        service = self.service
        receipt = service.receipt("/data", body)
        async with service.lock:
            replay = await service.replay(receipt)
            if replay is not None:
                return replay
            await self.invalidate()
            self.busy = True
            old_vault = service.document["meta"].get("plaid_vault")
            doc = {"state": defaults(service.clock()), "meta": {"fixture_session": False, "issued": []}}
            self.initialize(doc)
            doc["state"]["limitations"].append("Remote Sandbox Item removal unconfirmed.")
            await service.cancel_synthesis()
            await service.clear_checkpoints()
            await service.evaluate(doc)
            # Publish epoch rotation before waiting for remote cleanup.
            await service.commit(doc, erase=True)
            epoch = doc["state"]["data_epoch"]
            self.task = asyncio.create_task(self.call("erase", {"vault": old_vault}))
            task = self.task
        try:
            cleanup = await task
        except Exception:
            cleanup = {"confirmed": False}
        async with service.lock:
            if service.document["state"]["data_epoch"] != epoch:
                return deepcopy(service.document["state"])
            self.busy = False
            doc = deepcopy(service.document)
            if cleanup["confirmed"]:
                doc["state"]["limitations"] = [v for v in doc["state"]["limitations"] if v != "Remote Sandbox Item removal unconfirmed."]
            receipt["response"] = doc["state"]
            await service.commit(doc, receipt, erase=True)
            return deepcopy(receipt["response"])
