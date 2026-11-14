#!/usr/bin/env python3
"""Independent frozen-contract checks; no backend imports or network.

From project root:
  UV_CACHE_DIR=$PWD/.cache/verify/uv uv venv --python 3.12 .venv-verify
  UV_CACHE_DIR=$PWD/.cache/verify/uv uv pip install --python .venv-verify/bin/python 'jsonschema[format]>=4.23,<5'
  .venv-verify/bin/python -B scripts/verify_contract.py
"""
import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.dont_write_bytecode = True
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
SCHEMA = json.loads((CONTRACTS / "state.schema.json").read_text())
SCENARIOS = json.loads((CONTRACTS / "scenarios.json").read_text())
SOURCE_IDS = ["demo", "healthkit", "plaid", "email"]
CARD_IDS = ["purchase_recurrence", "recorded_steps", "preference_filter"]


def fixture(name):
    return json.loads((CONTRACTS / name).read_text())


def schema_errors(definition, value):
    schema = {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"],
              "$ref": f"#/$defs/{definition}"}
    # Never emit ValidationError.message/instance: they can contain private values.
    return [{"rule": e.validator, "schema_path": list(e.absolute_schema_path)}
            for e in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value)]


def ref_key(ref):
    return ref["observation_id"], ref["revision"]


def observation_issues(observations):
    issues = []
    for o in observations:
        try:
            zone = ZoneInfo(o["timezone"])
            if o["time_precision"] == "datetime":
                stamp = datetime.fromisoformat(o["occurred_at"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp.astimezone(zone).date().isoformat() != o["local_date"]:
                    issues.append("observation.timestamp_local_date")
        except (ZoneInfoNotFoundError, ValueError):
            issues.append("observation.timezone")
        if o["purchase"] and o["local_date"] != (o["purchase"]["authorized_date"] or o["purchase"]["posted_date"]):
            issues.append("observation.purchase_date")
        expected = {"demo": ("fixture", "demo_fixture", "synthetic"),
                    "healthkit": ("live", "healthkit_daily_steps", "daily_cumulative_sum")}
        if o["source_id"] not in expected or (o["mode"], o["provenance"]["adapter"], o["provenance"]["method"]) != expected.get(o["source_id"]):
            issues.append("observation.source_mode_provenance")
        if o["source_id"] == "healthkit" and o["kind"] != "activity":
            issues.append("observation.healthkit_kind")
    return issues


def state_issues(state):
    """Semantic checks beyond JSON Schema; return rule names, never payloads."""
    issues = observation_issues(state["observations"])

    def require(ok, rule):
        if not ok:
            issues.append(rule)

    prefs = state["preferences"]
    try:
        ZoneInfo(prefs["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        issues.append("preferences.timezone")
    require(len(set(prefs["enabled_source_ids"])) == len(prefs["enabled_source_ids"]), "preferences.unique_sources")
    require(len(set(prefs["suppressed_scenario_ids"])) == len(prefs["suppressed_scenario_ids"]), "preferences.unique_suppressions")
    sources = {s["id"]: s for s in state["sources"]}
    require([s["id"] for s in state["sources"]] == SOURCE_IDS, "sources.exact_order")
    for s in state["sources"]:
        require(s["enabled"] == (s["id"] in prefs["enabled_source_ids"]), "sources.enabled_mirror")
        if s["id"] in ("plaid", "email"):
            require(s["capability"] == "not_configured" and not s["enabled"], "sources.unconfigured")
    observations = state["observations"]
    keys = [(o["id"], o["revision"]) for o in observations]
    require(keys == sorted(set(keys)), "observations.unique_order")
    by_ref = dict(zip(keys, observations))
    superseded = {ref_key(o["supersedes"]) for o in observations if o["supersedes"]}
    require(superseded <= by_ref.keys(), "observations.supersedes_resolve")
    active = [o for o in observations if (o["id"], o["revision"]) not in superseded and o["operation"] == "upsert"]
    daily = [(o["source_id"], o["local_date"], o["timezone"]) for o in active if o["kind"] == "activity"]
    require(len(daily) == len(set(daily)), "observations.unique_daily_aggregate")
    cards = {c["id"]: c for c in state["ledger"]}
    require([c["id"] for c in state["ledger"]] == CARD_IDS, "ledger.exact_order")
    feedback_ids = {f["id"] for f in state["feedback"]}
    require(len(feedback_ids) == len(state["feedback"]), "feedback.unique_ids")
    for f in state["feedback"]:
        require(f["card_id"] in cards, "feedback.card_resolves")
        if f["event_ref"]:
            require(ref_key(f["event_ref"]) in by_ref, "feedback.evidence_resolves")
    day = date.fromisoformat(state["evaluation_date"])
    hk = sources.get("healthkit", {})
    step_source = "healthkit" if hk.get("enabled") and hk.get("consent") == "granted" else "demo"
    for c in state["ledger"]:
        cid = c["id"]
        require(cid == c["scenario_id"], "ledger.stable_id")
        refs = [ref_key(r) for r in c["evidence"]]
        require(len(refs) == len(set(refs)) and set(refs) <= by_ref.keys(), "ledger.evidence_resolves_unique")
        require(set(c["feedback_ids"]) <= feedback_ids, "ledger.feedback_resolves")
        start, end = day, day
        source = "demo"
        if cid == "purchase_recurrence":
            start = day - timedelta(days=SCENARIOS[cid]["window_days_including_today"] - 1)
        elif cid == "recorded_steps":
            start, end = day - timedelta(days=SCENARIOS[cid]["completed_local_days"]), day - timedelta(days=1)
            source = step_source
        require(c["window"] == {"start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": prefs["timezone"]}, "ledger.window")
        selected = []
        for o in active:
            s = sources.get(o["source_id"], {})
            if not (o["source_id"] == source and s.get("enabled") and s.get("consent") == "granted"
                    and o["timezone"] == prefs["timezone"] and start.isoformat() <= o["local_date"] <= end.isoformat()):
                continue
            if cid == "purchase_recurrence" and o["purchase"] and o["lifecycle"] == "posted" and not o["purchase"]["is_refund"] and o["purchase"]["category"] == SCENARIOS[cid]["category"]:
                selected.append(o)
            if cid == "recorded_steps" and o["activity"] and o["lifecycle"] == "recorded":
                selected.append(o)
        require(set(refs) == {(o["id"], o["revision"]) for o in selected}, "ledger.exact_active_evidence")
        if selected:
            require({o["mode"] for o in selected} == {c["mode"]}, "ledger.no_mixed_mode")
        if cid == "purchase_recurrence":
            require(c["purchase_count"] == len(selected) and c["distinct_purchase_dates"] == len({o["local_date"] for o in selected}), "ledger.purchase_counts")
            require(c["recorded_steps"] is None and c["days_with_records"] is None, "ledger.purchase_nulls")
        elif cid == "recorded_steps":
            require(c["recorded_steps"] == (sum(o["activity"]["value"] for o in selected) if selected else None)
                    and c["days_with_records"] == len({o["local_date"] for o in selected}), "ledger.step_counts")
            require(c["purchase_count"] is None and c["distinct_purchase_dates"] is None and c["inferred"] is None, "ledger.step_nulls")
            if not selected:
                require(c["status"] == "insufficient_evidence" and c["coverage"] == "unknown" and c["suggestion"] is None
                        and c["observed"] == SCENARIOS[cid]["empty_text"], "ledger.empty_steps_unknown")
        else:
            require(all(c[k] is None for k in ("purchase_count", "distinct_purchase_dates", "recorded_steps", "days_with_records", "inferred", "suggestion")), "ledger.preference_nulls")
        suggestion = c["suggestion"]
        if suggestion:
            require(suggestion["id"] == SCENARIOS[cid].get("suggestion_id"), "suggestion.stable_id")
            blocked = (cid in prefs["suppressed_scenario_ids"]
                       or (cid == "purchase_recurrence" and not prefs["food_suggestions"])
                       or (cid == "recorded_steps" and not prefs["activity_suggestions"])
                       or (suggestion["requires_movement"] and prefs["movement_restriction"])
                       or (suggestion["requires_kitchen"] and prefs["no_kitchen"])
                       or (suggestion["requires_spending"] and prefs["no_spending"]))
            require(not blocked or suggestion["status"] != "eligible", "suggestion.constraints")
    intents = state["notification_intents"]
    require(prefs["notifications"]["enabled"] or not intents, "notifications.disabled_empty")
    require(len(intents) <= SCENARIOS["notifications"]["maximum_new_intents_per_local_day"], "notifications.bounded")
    for intent in intents:
        c = cards.get(intent["card_id"], {})
        require(c.get("suggestion") is not None and c["suggestion"]["status"] == "eligible" and intent["scenario_id"] == c.get("scenario_id"), "notifications.eligible_card")
    for draft in state["process"]["synthesis"]["drafts"]:
        require(set(draft["card_ids"]) <= cards.keys(), "draft.card_resolves")
        require({ref_key(r) for r in draft["evidence"]} <= by_ref.keys(), "draft.evidence_resolves")
    return sorted(set(issues))


def write_report(output, report):
    path = Path(output)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("output must remain under project root")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default="evidence/contract-check.json", help="Project-local sanitized JSON evidence")
    args = parser.parse_args()
    results = []

    def check(name, expected, actual):
        results.append({"check": name, "passed": expected == actual, "expected": expected, "actual": actual})

    Draft202012Validator.check_schema(SCHEMA)
    check("schema.draft_2020_12", "https://json-schema.org/draft/2020-12/schema", SCHEMA["$schema"])
    state, batch = fixture("state.example.json"), fixture("observation-batch.example.json")
    check("positive.state", [], schema_errors("state", state))
    check("positive.observation_batch", [], schema_errors("observation_batch", batch))
    check("positive.state_semantics", [], state_issues(state))
    check("positive.batch_semantics", [], observation_issues(batch["observations"]))
    check("fixtures.same_observations", True, batch["observations"] == state["observations"])
    check("fixtures.evaluation_date", SCENARIOS["demo"]["evaluation_date"], state["evaluation_date"])
    steps = [o for o in state["observations"] if o["activity"]]
    purchases = [o for o in state["observations"] if o["purchase"]]
    dates = sorted(date.fromisoformat(o["local_date"]) for o in purchases)
    counts = {"observations": len(state["observations"]), "recorded_steps": sum(o["activity"]["value"] for o in steps),
              "step_dates": len({o["local_date"] for o in steps}), "purchases": len(purchases),
              "purchase_dates": len(set(dates)), "purchase_span_days": (dates[-1] - dates[0]).days,
              "notifications": len(state["notification_intents"])}
    check("fixtures.acceptance_counts", {"observations": 6, "recorded_steps": 13800, "step_dates": 3,
          "purchases": 3, "purchase_dates": 3, "purchase_span_days": 19, "notifications": 0}, counts)
    check("fixtures.movement_suppressed", "suppressed", state["ledger"][1]["suggestion"]["status"])
    negatives = [
        ("invalid_date", "observation", purchases[0], ("local_date",), "2026-02-30"),
        ("invalid_unit", "observation", steps[0], ("activity", "unit"), "steps"),
        ("unknown_property", "state", state, ("raw_payload",), "synthetic"),
        ("nested_unknown_property", "observation", purchases[0], ("purchase", "raw_payload"), "synthetic"),
        ("wrong_null_payload", "observation", purchases[0], ("purchase",), None),
        ("wrong_null_collection", "state", state, ("observations",), None),
        ("wrong_null_notes", "state", state, ("preferences", "notes"), None),
        ("date_with_timestamp", "observation", purchases[0], ("occurred_at",), "2026-10-22T12:00:00Z"),
        ("wrong_version", "observation_batch", batch, ("schema_version",), "2"),
        ("kind_payload_mismatch", "observation", steps[0], ("kind",), "purchase"),
        ("boolean_steps", "observation", steps[0], ("activity", "value"), True),
        ("negative_steps", "observation", steps[0], ("activity", "value"), -1),
    ]
    for name, definition, original, path, value in negatives:
        bad = deepcopy(original)
        target = bad
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        check("negative." + name, True, bool(schema_errors(definition, bad)))
    bad = deepcopy(purchases[0])
    del bad["occurred_at"]
    check("negative.omitted_nullable", True, bool(schema_errors("observation", bad)))
    bad = deepcopy(purchases[0])
    bad.update(time_precision="datetime", occurred_at="2026-10-22T12:00:00")
    check("negative.timestamp_without_offset", True, bool(schema_errors("observation", bad)))
    for name, path, value, rule in [
        ("dangling_evidence", ("ledger", 0, "evidence", 0, "revision"), 99, "ledger.evidence_resolves_unique"),
        ("wrong_step_count", ("ledger", 1, "recorded_steps"), 0, "ledger.step_counts"),
        ("wrong_purchase_count", ("ledger", 0, "purchase_count"), 4, "ledger.purchase_counts"),
        ("invalid_timezone", ("preferences", "timezone"), "Not/AZone", "preferences.timezone"),
        ("configured_plaid", ("sources", 2, "capability"), "available", "sources.unconfigured"),
        ("movement_not_suppressed", ("ledger", 1, "suggestion", "status"), "eligible", "suggestion.constraints"),
        ("purchase_date_mismatch", ("observations", 0, "local_date"), "2026-10-23", "observation.purchase_date"),
        ("mixed_mode", ("observations", 0, "mode"), "live", "ledger.no_mixed_mode"),
    ]:
        bad = deepcopy(state)
        target = bad
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        check("negative.semantic." + name, True, rule in state_issues(bad))
    failed = sum(not r["passed"] for r in results)
    report = {"status": "failed" if failed else "passed", "generated_at": datetime.now(timezone.utc).isoformat(),
              "scope": "Static contract fixtures only; HTTP, SDK execution, iOS and live providers are not verified.",
              "inputs_sha256": {p.name: sha256(p.read_bytes()).hexdigest() for p in sorted(CONTRACTS.iterdir()) if p.is_file()},
              "summary": {"checks": len(results), "passed": len(results) - failed, "failed": failed, "fixture_counts": counts}, "checks": results}
    write_report(args.output, report)
    print(json.dumps({"status": report["status"], **report["summary"]}))
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
