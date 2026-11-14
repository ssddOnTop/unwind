"""Transparent product-rule evaluation, never a clinical assessment."""
from copy import deepcopy
from datetime import date, timedelta

from ..contracts import RULES
from ..templates import CARDS, SUGGESTIONS
from ..domain import active, instant, ref, refkey, zone


class ScenariosExtension:
    id, version, status = "scenarios", "1", "available"

    async def handle(self, operation: str, payload: dict) -> dict:
        if operation != "evaluate":
            raise ValueError("Unsupported scenario operation")
        s = deepcopy(payload["state"])
        p = s["preferences"]
        today = date.fromisoformat(s["evaluation_date"])
        sources = {v["id"]: v for v in s["sources"]}
        for source in s["sources"]:
            if source["enabled"] and source["consent"] == "granted" and source["last_success_at"] and source["status"] not in {"error", "empty"}:
                last = instant(source["last_success_at"]).astimezone(zone(p["timezone"])).date()
                if last < today - timedelta(days=1):
                    source["status"] = "stale"
        usable = [o for o in active(s["observations"]) if sources[o["source_id"]]["enabled"] and sources[o["source_id"]]["consent"] == "granted"]
        wrong_zone = any(o["timezone"] != p["timezone"] for o in usable)
        usable = [o for o in usable if o["timezone"] == p["timezone"]]
        cards = deepcopy(CARDS)
        old = {c["id"]: c for c in s["ledger"]}
        for c in cards:
            c.update(revision=1, evidence=[], feedback_ids=[], suggestion=None, inferred=None,
                     purchase_count=None, distinct_purchase_dates=None, recorded_steps=None,
                     days_with_records=None, coverage="unknown", status="insufficient_evidence")
            c["window"]["timezone"] = p["timezone"]
        purchase, steps, pref = cards
        start = today - timedelta(days=RULES["purchase_recurrence"]["window_days_including_today"] - 1)
        purchase["window"].update(start_date=start.isoformat(), end_date=today.isoformat())
        purchase_source = "plaid" if payload.get("purchase_source_id") == "plaid" and sources["plaid"]["enabled"] and sources["plaid"]["consent"] == "granted" else "demo"
        rows = [o for o in usable if o["source_id"] == purchase_source and o["kind"] == "purchase" and o["lifecycle"] == "posted"
                and not o["purchase"]["is_refund"] and o["purchase"]["category"] == RULES["purchase_recurrence"]["category"]
                and start.isoformat() <= o["local_date"] <= today.isoformat()]
        dates = sorted({o["local_date"] for o in rows})
        purchase.update(purchase_count=len(rows), distinct_purchase_dates=len(dates), evidence=[ref(o) for o in rows])
        def recurrent(ds):
            return len(ds) >= RULES["purchase_recurrence"]["minimum_distinct_dates"] and (date.fromisoformat(max(ds)) - date.fromisoformat(min(ds))).days >= RULES["purchase_recurrence"]["minimum_span_days"]
        if rows:
            purchase.update(status="observed", coverage="partial",
                observed=f"Between {start} and {today}, {'synthetic Plaid Sandbox' if purchase_source == 'plaid' else 'synthetic connected'} records show {len(rows)} purchases at merchants categorized as restaurant, on {len(dates)} dates. This describes purchases, not meals or what you ate. Other purchases may be missing.")
        else:
            purchase["observed"] = RULES["purchase_recurrence"]["insufficient_text"]
        if purchase_source == "plaid":
            purchase["limitations"].append("Synthetic Plaid Sandbox transactions only; bank categories are not consumption evidence.")
        if recurrent(dates):
            purchase["inferred"] = "These records meet the product recurrence rule; they do not establish a dietary habit."
        eligible_dates = []
        for o in rows:
            corrections = [f for f in s["feedback"] if f["kind"] == "correctness" and f["scope"] == "event" and f["event_ref"] == ref(o)]
            latest = corrections[-1] if corrections else None
            attribution = o["household_attribution"]
            if latest and latest["attribution"] is not None:
                attribution = latest["attribution"]
            if (latest is None or latest["value"] != "incorrect") and attribution == "self":
                eligible_dates.append(o["local_date"])
        if len(eligible_dates) != len(rows):
            purchase["limitations"].append("Shared, unknown, other, or disputed transaction context cannot establish personal suggestion eligibility.")
        if recurrent(set(eligible_dates)):
            purchase["suggestion"] = deepcopy(SUGGESTIONS["purchase_recurrence"])
        start, end = today - timedelta(days=RULES["recorded_steps"]["completed_local_days"]), today - timedelta(days=1)
        steps["window"].update(start_date=start.isoformat(), end_date=end.isoformat())
        source_id = "healthkit" if sources["healthkit"]["enabled"] and sources["healthkit"]["consent"] == "granted" else "demo"
        steps["mode"] = "live" if source_id == "healthkit" else "fixture"
        rows = [o for o in usable if o["source_id"] == source_id and o["kind"] == "activity" and start.isoformat() <= o["local_date"] <= end.isoformat()]
        days = len({o["local_date"] for o in rows})
        steps.update(days_with_records=days, evidence=[ref(o) for o in rows], observed=RULES["recorded_steps"]["empty_text"])
        if rows:
            total = sum(o["activity"]["value"] for o in rows)
            label = "synthetic connected" if source_id == "demo" else "connected"
            steps.update(status="observed", recorded_steps=total,
                observed=f"From {start} through {end}, {label} records contain {total} steps on {days} of 7 dates. That does not mean those dates were fully tracked. Unrecorded activity is unknown.")
            steps["suggestion"] = deepcopy(SUGGESTIONS["recorded_steps"])
            steps["suggestion"].update(status="eligible", reason=None)
        for c in cards[:2]:
            relevant = [f for f in s["feedback"] if f["card_id"] == c["id"] and (f["event_ref"] is None or f["event_ref"] in c["evidence"])]
            c["feedback_ids"] = [f["id"] for f in relevant]
            if wrong_zone:
                c["limitations"].append("Records in a different timezone are excluded until the adapter supplies reaggregated revisions.")
            candidate = c["suggestion"]
            if candidate:
                reason = None
                if c["id"] in p["suppressed_scenario_ids"]:
                    reason = "scenario_suppressed"
                elif c["id"] == "purchase_recurrence" and not p["food_suggestions"]:
                    reason = "food_suggestions_disabled"
                elif c["id"] == "recorded_steps" and (p["movement_restriction"] or not p["activity_suggestions"]):
                    reason = "movement_restriction" if p["movement_restriction"] else "activity_suggestions_disabled"
                elif candidate["requires_kitchen"] and p["no_kitchen"]:
                    reason = "no_kitchen"
                elif candidate["requires_spending"] and p["no_spending"]:
                    reason = "no_spending"
                if reason:
                    candidate.update(status="suppressed", reason=reason)
                else:
                    for kind, positive in (("correctness", "correct"), ("usefulness", "useful")):
                        fs = [f for f in relevant if f["kind"] == kind and f["scope"] == "card"]
                        if fs and fs[-1]["value"] != positive:
                            candidate.update(status="unavailable", reason=f"{kind}_feedback")
                    disputed = [f for f in relevant if f["kind"] == "correctness" and f["scope"] == "event"]
                    last_by_event = {refkey(f["event_ref"]): f for f in disputed}
                    if c["id"] == "recorded_steps" and any(f["value"] != "correct" for f in last_by_event.values()):
                        candidate.update(status="unavailable", reason="correctness_feedback")
        pref.update(status="observed", window={"start_date": today.isoformat(), "end_date": today.isoformat(), "timezone": p["timezone"]})
        if p["movement_restriction"]:
            pref["observed"] = "You asked us to avoid movement suggestions. Recorded observations are unchanged."
        else:
            pref["observed"] = "Your explicit preferences apply immediately. Recorded observations are unchanged."
        if not any(c["suggestion"] and c["suggestion"]["status"] == "eligible" for c in cards[:2]):
            pref["observed"] += " " + RULES["preference_filter"]["no_compatible_text"]
        for c in cards:
            c["evidence"].sort(key=lambda r: (r["observation_id"], r["revision"]))
            if c["id"] in old:
                previous = deepcopy(old[c["id"]])
                revision = previous.pop("revision")
                semantic = {k: v for k, v in c.items() if k != "revision"}
                c["revision"] = revision + int(previous != semantic)
        s["ledger"] = cards
        return {"state": s}
