"""Optional elapsed-time synthesis; durable attempts belong to service.process."""
from datetime import timedelta, timezone
import hashlib
import json
import os

from .contracts import DomainError
from .domain import instant, key, refkey, utcnow


class DailyCoordinator:
    def __init__(self, service, mode=None, clock=utcnow):
        self.service, self.clock = service, clock
        self.mode = os.getenv("COMPANION_DAILY_SYNTHESIS_MODE", "disabled") if mode is None else mode
        if self.mode not in {"disabled", "scripted", "live"}:
            raise ValueError("Daily synthesis mode must be disabled, scripted, or live")

    async def tick(self):
        """Return the process response when due, otherwise None; call after housekeep."""
        if self.mode == "disabled":
            return None
        state = await self.service.state()
        process = state["process"]
        if process["status"] == "running":
            return None
        try:
            now = self.clock()
            if now.utcoffset() is None:
                return None
            now = now.astimezone(timezone.utc)
            started = process["started_at"]
            if started is not None:
                last = instant(started)
                if last.utcoffset() is None or now - last.astimezone(timezone.utc) < timedelta(hours=24):
                    return None
            elif process["status"] != "idle" or process["run_id"] is not None:
                return None  # An ambiguous prior attempt must not trigger a cost burst.
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        observations = state["observations"]
        if self.mode == "scripted" and any(o["mode"] != "fixture" for o in observations):
            return None  # Even historical/revoked live input is not a scripted fixture.
        sources = {s["id"] for s in state["sources"] if s["enabled"] and s["consent"] == "granted"}
        evidence = {key(o) for o in observations if o["source_id"] in sources and o["operation"] == "upsert"}
        if not any(card["evidence"] and all(refkey(r) in evidence for r in card["evidence"])
                   and card["scenario_id"] not in state["preferences"]["suppressed_scenario_ids"]
                   and card["suggestion"] and card["suggestion"]["status"] == "eligible"
                   for card in state["ledger"]):
            return None
        # Advance the receipt key after EVERY recorded attempt (including failed/skipped).
        # Hashing bounds IDs and avoids clock/date collisions and infinite old replays.
        token = json.dumps([state["data_epoch"], self.mode, process["run_id"], started])
        body = {"schema_version": "1", "data_epoch": state["data_epoch"],
                "request_id": "daily:" + hashlib.sha256(token.encode()).hexdigest(),
                "synthesis_mode": self.mode}
        try:
            return await self.service.process(body)
        except DomainError as exc:
            if exc.code not in {"busy", "epoch_mismatch", "invalid_request", "consent_required"}:
                raise
            return None  # Concurrent processing, deletion, or source changes: recheck next tick.
