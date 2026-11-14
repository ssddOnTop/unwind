"""Shared public pure domain helpers. No extension implementation imports."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import DomainError, EXAMPLE, RULES


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise DomainError() from None


def ref(o):
    return {"observation_id": o["id"], "revision": o["revision"]}


def key(o):
    return o["id"], o["revision"]


def refkey(r):
    return r["observation_id"], r["revision"]


def synthesis(mode="disabled", status="idle", reason=None):
    return {"mode": mode, "status": status, "reason": reason, "engine": "deepagents", "drafts": []}


def defaults(now):
    s = deepcopy(EXAMPLE)
    s.update(data_epoch=str(uuid4()), revision=0, generated_at=iso(now), observations=[],
             feedback=[], notification_intents=[], ledger=[])
    p = s["preferences"]
    p.update(no_kitchen=False, no_spending=False, movement_restriction=False, notes="")
    for source in s["sources"]:
        source.update(consent="not_requested", last_attempt_at=None, last_success_at=None,
                      coverage="unknown", coverage_window=None)
        if source["id"] == "demo":
            source.update(status="ready", reason="Synthetic adapter; explicit consent required.")
    s["process"] = {"run_id": None, "status": "idle", "started_at": None, "completed_at": None,
                    "input_state_revision": 0, "ledger_status": "idle", "synthesis": synthesis()}
    s["evaluation_date"] = now.astimezone(zone(p["timezone"])).date().isoformat()
    s["limitations"] = ["No diagnoses, obesity predictions, intake estimates, or clinical thresholds.",
                         "Physical-device HealthKit access and OS notification delivery are not verified."]
    return s


def active(observations):
    superseded = {refkey(o["supersedes"]) for o in observations if o["supersedes"]}
    latest = {}
    for o in observations:
        if o["id"] not in latest or o["revision"] > latest[o["id"]]["revision"]:
            latest[o["id"]] = o
    return [o for o in latest.values() if key(o) not in superseded and o["operation"] != "remove"]


def consent(state, source_id):
    source = next(s for s in state["sources"] if s["id"] == source_id)
    if source["capability"] != "available":
        raise DomainError("not_configured", 409)
    if not source["enabled"] or source["consent"] != "granted":
        raise DomainError("consent_required", 409)
    return source


def preference_update(state, preferences):
    zone(preferences["timezone"])
    for field in ("enabled_source_ids", "suppressed_scenario_ids"):
        if len(preferences[field]) != len(set(preferences[field])):
            raise DomainError()
    if {"plaid", "email"} & set(preferences["enabled_source_ids"]):
        raise DomainError("not_configured", 409)
    state["preferences"] = deepcopy(preferences)
    for source in state["sources"]:
        enabled = source["id"] in preferences["enabled_source_ids"]
        if enabled:
            if not source["enabled"] or source["consent"] != "granted":
                source.update(status="ready", reason=None)
            source["consent"] = "granted"
        elif source["enabled"] or source["consent"] == "granted":
            source.update(consent="revoked", status="disabled", coverage="unknown", reason="App consent revoked.")
        source["enabled"] = enabled


def prune(state, now):
    d = now.astimezone(zone(state["preferences"]["timezone"])).date()
    cutoff = (d - timedelta(days=RULES["retention"]["observation_days"] - 1)).isoformat()
    old = state["observations"]
    state["observations"] = [o for o in old if o["local_date"] >= cutoff]
    refs = {key(o) for o in state["observations"]}
    state["feedback"] = [f for f in state["feedback"] if f["event_ref"] is None or refkey(f["event_ref"]) in refs]
    return len(old) != len(state["observations"])
