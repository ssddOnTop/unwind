"""Replaceable notification planner; returns desired intents and issuance history."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

from .contracts import RULES
from .domain import instant, iso, zone


class NotificationScheduler:
    async def handle(self, operation: str, payload: dict) -> dict:
        if operation != "plan":
            raise ValueError("Unsupported scheduling operation")
        s, issued, now = payload["state"], deepcopy(payload["issued"]), instant(payload["now"])
        p, d = s["preferences"], date.fromisoformat(s["evaluation_date"])
        tz = zone(p["timezone"])
        eligible = {c["id"]: c for c in s["ledger"] if c["suggestion"] and c["suggestion"]["status"] == "eligible"}
        if not p["notifications"]["enabled"]:
            return {"intents": [], "issued": issued}
        naive = datetime.combine(d, time.fromisoformat(p["notifications"]["local_time"]))
        when = naive.replace(tzinfo=tz, fold=0)
        # Round-trip rejects nonexistent spring-forward wall times. fold=0 selects first repeat.
        valid = when.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive
        expires = datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz)
        intents = [i for i in s["notification_intents"] if i["scenario_id"] in eligible and
                   instant(i["scheduled_at"]) > now and instant(i["scheduled_at"]) == when and valid]
        if intents or not valid or when <= now or any(i["date"] == d.isoformat() for i in issued):
            return {"intents": intents, "issued": issued}
        for sid in RULES["notifications"]["priority"]:
            if sid not in eligible:
                continue
            suggestion_id = eligible[sid]["suggestion"]["id"]
            if any(i["suggestion_id"] == suggestion_id and (d - date.fromisoformat(i["date"])).days < RULES["notifications"]["same_suggestion_cooldown_days"] for i in issued):
                continue
            identifier = f"{sid}:{d}"
            issued.append({"id": identifier, "suggestion_id": suggestion_id, "date": d.isoformat()})
            intents.append({"id": identifier, "scenario_id": sid, "card_id": sid,
                            "scheduled_at": iso(when), "expires_at": iso(expires),
                            "title": "Your companion", "body": "An optional suggestion is ready to review."})
            break
        return {"intents": intents, "issued": issued}
