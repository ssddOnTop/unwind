"""Atomic immutable evidence admission, independent of storage implementation."""
from copy import deepcopy
from datetime import date, timedelta

from .contracts import DomainError, RULES
from .domain import active, consent, instant, key, ref, refkey, zone


def validate_observation(o, evaluation_date):
    tz = zone(o["timezone"])
    if o["local_date"] < (date.fromisoformat(evaluation_date) - timedelta(days=RULES["retention"]["observation_days"] - 1)).isoformat():
        raise DomainError()
    if o["time_precision"] == "datetime" and instant(o["occurred_at"]).astimezone(tz).date().isoformat() != o["local_date"]:
        raise DomainError()
    expected = {"demo": ("fixture", "demo_fixture", "synthetic"),
                "healthkit": ("live", "healthkit_daily_steps", "daily_cumulative_sum")}
    if o["source_id"] not in expected:
        raise DomainError("not_configured", 409)
    mode, adapter, method = expected[o["source_id"]]
    if (o["mode"], o["provenance"]["adapter"], o["provenance"]["method"]) != (mode, adapter, method):
        raise DomainError()
    if o["source_id"] == "healthkit" and o["kind"] != "activity":
        raise DomainError()
    if o["operation"] == "remove":
        if o["supersedes"] is None:
            raise DomainError("revision_conflict", 409)
    elif o["kind"] == "purchase":
        p = o["purchase"]
        if (p["authorized_date"] or p["posted_date"]) != o["local_date"]:
            raise DomainError()
        if o["lifecycle"] == "posted" and p["posted_date"] is None:
            raise DomainError()


def validate_update(u):
    zone(u["coverage_window"]["timezone"])
    if u["coverage_window"]["start_date"] > u["coverage_window"]["end_date"]:
        raise DomainError()
    if (u["query_status"] == "error") != (u["error_code"] is not None):
        raise DomainError()
    if u["source_id"] == "healthkit":
        if u["read_access"] == "not_applicable" or (u["query_status"] != "success" and u["read_access"] != "unknown"):
            raise DomainError()
    elif u["read_access"] != "not_applicable":
        raise DomainError()


async def ingest(registry, state, body):
    observations = deepcopy(state["observations"])
    by_key = {key(o): o for o in observations}
    latest = {}
    for o in observations:
        if o["id"] not in latest or o["revision"] > latest[o["id"]]["revision"]:
            latest[o["id"]] = o
    accepted = ignored = 0
    for o in body["observations"]:
        consent(state, o["source_id"])
        await registry.dispatch(o["source_id"], "validate_observation", {"observation": o, "evaluation_date": state["evaluation_date"]})
        k = key(o)
        if k in by_key:
            if by_key[k] != o:
                raise DomainError("revision_conflict", 409)
            ignored += 1
            continue
        prior = latest.get(o["id"])
        if prior:
            if any(prior[f] != o[f] for f in ("source_id", "mode", "kind")):
                raise DomainError("revision_conflict", 409)
            if o["revision"] < prior["revision"]:
                ignored += 1
                continue
            if o["revision"] != prior["revision"] + 1 or o["supersedes"] != ref(prior):
                raise DomainError("revision_conflict", 409)
        elif o["revision"] != 1:
            raise DomainError("revision_conflict", 409)
        by_key[k] = deepcopy(o)
        latest[o["id"]] = o
        observations.append(deepcopy(o))
        accepted += 1
    successors = {}
    for o in observations:
        if o["supersedes"] is None:
            continue
        target = refkey(o["supersedes"])
        previous = by_key.get(target)
        if previous is None:
            # Retention can erase an old predecessor; new incoming links must always resolve.
            if key(o) not in {key(v) for v in state["observations"]}:
                raise DomainError("revision_conflict", 409)
            continue
        if any(o[f] != previous[f] for f in ("source_id", "mode", "kind")):
            raise DomainError("revision_conflict", 409)
        if previous["id"] != o["id"] and not (o["revision"] == 1 and o["kind"] == "purchase" and previous["lifecycle"] == "pending" and o["lifecycle"] == "posted"):
            raise DomainError("revision_conflict", 409)
        if target in successors and successors[target] != key(o):
            raise DomainError("revision_conflict", 409)
        successors[target] = key(o)
        if o["operation"] == "remove":
            for f in ("local_date", "timezone", "time_precision", "occurred_at", "provenance", "household_attribution"):
                if o[f] != previous[f]:
                    raise DomainError("revision_conflict", 409)
    for start in successors:
        seen = set()
        cursor = start
        while cursor in successors:
            if cursor in seen:
                raise DomainError("revision_conflict", 409)
            seen.add(cursor)
            cursor = successors[cursor]
    daily = set()
    for o in active(observations):
        if o["kind"] == "activity":
            identity = (o["source_id"], o["local_date"], o["timezone"])
            if identity in daily:
                raise DomainError("revision_conflict", 409)
            daily.add(identity)
    updates = body["source_updates"]
    if len({u["source_id"] for u in updates}) != len(updates):
        raise DomainError()
    for u in updates:
        source = consent(state, u["source_id"])
        await registry.dispatch(u["source_id"], "validate_update", u)
        if source["last_attempt_at"] and instant(u["attempted_at"]) <= instant(source["last_attempt_at"]):
            continue
        source.update(last_attempt_at=u["attempted_at"], authorization_requested=u["authorization_requested"],
                      read_access=u["read_access"], coverage_window=deepcopy(u["coverage_window"]),
                      status={"success": "ready", "empty": "empty", "error": "error"}[u["query_status"]],
                      coverage="partial" if u["query_status"] == "success" else "unknown",
                      reason=u["error_code"])
        if u["query_status"] != "error":
            source["last_success_at"] = u["attempted_at"]
    state["observations"] = sorted(observations, key=key)
    return {"accepted": accepted, "ignored": ignored}
