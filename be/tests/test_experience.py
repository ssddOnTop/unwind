"""Backend ports of visit/Home safety cases; no native inference or provider calls."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from companion.contracts import EXAMPLE, RULES
from companion.domain import iso, ref
from companion.experience_contracts import EXPERIENCE_SCHEMA, validate_experience
from companion.extensions.experience import project, ExperienceExtension, DEMO_LABEL
from test_backend import NOW


def snapshot():
    return deepcopy(EXAMPLE)


def projection(state=None, answers=None, **extra):
    state = snapshot() if state is None else state
    before = deepcopy(state)
    result = project({"state": state, "now": iso(NOW), "as_of_date": "2026-11-14", "answers": answers or [], **extra})
    assert state == before
    validate_experience("experience", result["experience"])
    return result


def view(state=None, **extra):
    return projection(state, **extra)["experience"]


def response(state, answer="yes", created_at=None):
    result = projection(state)
    return [{**result["question_scope"], "answer": answer, "created_at": created_at or iso(NOW), "data_epoch": state["data_epoch"]}]


def feedback(state, value="incorrect", attribution=None, scope="event", kind="correctness"):
    state["feedback"].append({"id": "private-feedback-" + str(len(state["feedback"])), "card_id": "purchase_recurrence",
        "kind": kind, "value": value, "scope": scope, "event_ref": ref(state["observations"][0]) if scope == "event" else None,
        "attribution": attribution, "note": "PRIVATE_FEEDBACK", "created_at": iso(NOW)})


def test_no_clinical_concern_is_inferred_from_purchases_or_partial_steps():
    result = view()
    assert result["visit"]["status"] == "inferred_pattern"
    assert result["visit"]["record_review"] == "limited"
    assert result["visit"]["confirmed_context"] == []
    for word in ("healthy", "bad eating habits", "low activity", "you ate", "diabetes", "heart disease", "specialist needed", "disease risk is"):
        assert word not in json.dumps({"home": result["home"], "visit": result["visit"]}).lower()
    assert "not clinical concerns" in result["visit"]["brief"]


def test_partial_coverage_uses_actual_counts_windows_and_no_zero_imputation():
    lines = view()["visit"]["observed_information"]
    assert len(lines) == 2
    assert "2026-10-18 through 2026-11-14" in lines[0]
    assert "3 restaurant-category purchase records on 3 dates" in lines[0]
    assert "2026-11-07 through 2026-11-13" in lines[1]
    assert "13800 recorded steps across 3 dates" in lines[1]
    assert "Missing activity records are unknown, not zero" in view()["visit"]["brief"]


def test_changed_counts_and_windows_are_not_canned_demo_facts():
    state = snapshot()
    state["observations"][3]["activity"]["value"] = 100
    state["ledger"][1]["recorded_steps"] = 9700
    state["ledger"][1]["window"]["start_date"] = "2026-11-08"
    lines = view(state)["visit"]["observed_information"]
    assert "9700 recorded steps" in lines[1]
    assert "2026-11-08 through 2026-11-13" in lines[1]


def test_tap_confirmed_context_and_preferences_stay_separate_from_observations():
    state = snapshot()
    result = view(state, answers=response(state))
    assert "mostly for me" in result["visit"]["confirmed_context"][0]
    assert not any("mostly for me" in s for s in result["visit"]["observed_information"])
    assert any("no kitchen" in s for s in result["visit"]["user_reported_preferences"])
    assert any("no-spending" in s for s in result["visit"]["user_reported_preferences"])
    assert not any("movement" in s for s in result["visit"]["user_reported_preferences"])
    assert "nonurgent" in result["visit"]["doctor_starting_point"]


def test_movement_preference_preserves_actual_steps_without_diagnosing_condition():
    state = snapshot()
    state["ledger"] = state["ledger"][1:]
    result = view(state)
    assert "13800 recorded steps" in result["visit"]["observed_information"][0]
    assert result["home"]["primary_action"]["kind"] == "view_ledger"
    assert result["home"]["status"] == "suppressed"
    assert result["visit"]["confirmed_context"] == []


@pytest.mark.parametrize("attribution", ["shared", "other", "unknown"])
def test_shared_other_unknown_are_never_personal_certainty(attribution):
    state = snapshot()
    for o in state["observations"]:
        o["household_attribution"] = attribution
    result = view(state)
    assert bool(result["visit"]["inferred_habits"]) == (attribution == "unknown")
    assert len(result["visit"]["observed_information"]) == 2
    assert "not confirmed personal eating or activity" in result["visit"]["brief"]
    assert all("your" not in line.lower() for line in result["visit"]["observed_information"])


@pytest.mark.parametrize("status", ["disabled", "syncing", "empty", "stale", "error", "not_configured"])
def test_missing_disconnected_and_unready_records_do_not_clear(status):
    state = snapshot()
    state["sources"][0]["status"] = status
    result = view(state)
    assert result["visit"]["observed_information"] == []
    assert result["visit"]["record_review"] == "not_reviewed"
    assert result["home"]["status"] != "no_flags"


@pytest.mark.parametrize("change", ["no_sources", "revoked", "disabled_preferences", "old_success"])
def test_source_authority_is_rechecked(change):
    state = snapshot()
    if change == "no_sources": state["sources"] = []
    elif change == "revoked": state["sources"][0]["consent"] = "revoked"
    elif change == "old_success": state["sources"][0]["last_success_at"] = "2026-10-01T00:00:00Z"
    else: state["preferences"]["enabled_source_ids"] = []
    assert view(state)["visit"]["observed_information"] == []


@pytest.mark.parametrize("when", ["2026-11-15", "not-a-date"])
def test_stale_or_invalid_evaluation_date_never_reviews(when):
    result = view(as_of_date=when)
    assert result["visit"]["record_review"] == "not_reviewed"
    assert result["clarification"] is None


@pytest.mark.parametrize("change", ["missing", "running", "failed"])
def test_missing_or_unverified_running_or_failed_ledger_does_not_clear(change):
    state = snapshot()
    if change == "missing": state["ledger"] = []
    elif change == "running": state["process"]["status"] = "running"
    else: state["process"]["ledger_status"] = "failed"
    assert view(state)["visit"]["record_review"] == "not_reviewed"


def test_suppression_cannot_become_all_clear():
    state = snapshot()
    state["preferences"]["suppressed_scenario_ids"] = ["purchase_recurrence", "recorded_steps"]
    result = view(state)
    assert result["home"]["status"] == "suppressed"
    assert result["visit"]["observed_information"] == []
    assert "hidden items do not mean there are no concerns" in result["visit"]["brief"]


@pytest.mark.parametrize("change", ["missing_ref", "new_revision", "successor", "pending", "removed", "operation", "refund"])
def test_old_superseded_removed_pending_and_refund_records_excluded(change):
    state = snapshot()
    row = state["observations"][0]
    if change == "missing_ref": state["ledger"][0]["evidence"][0]["revision"] = 99
    elif change in {"new_revision", "successor"}:
        successor = deepcopy(row)
        if change == "new_revision": successor["revision"] = 2
        else: successor.update(id="successor", supersedes=ref(row))
        state["observations"].append(successor)
    elif change in {"pending", "removed"}: row["lifecycle"] = change
    elif change == "operation": row["operation"] = "remove"
    else: row["purchase"]["is_refund"] = True
    assert len(view(state)["visit"]["observed_information"]) == 1


@pytest.mark.parametrize("change", ["totals", "refs", "duplicate", "overlap", "overflow"])
def test_mismatched_totals_duplicate_references_overlapping_steps_and_overflow(change):
    state = snapshot()
    if change == "totals":
        state["ledger"][0]["purchase_count"] = 100
        state["ledger"][1]["recorded_steps"] = 999999
    elif change == "refs": state["ledger"][0]["evidence"].append(state["ledger"][0]["evidence"][0])
    elif change == "duplicate": state["observations"].append(state["observations"][0])
    elif change == "overlap": state["observations"][4]["local_date"] = state["observations"][3]["local_date"]
    else: state["observations"][3]["activity"]["value"] = 2**63 - 1
    assert len(view(state)["visit"]["observed_information"]) == (0 if change == "totals" else 1)


def test_disputed_card_blocks_only_relevant_summary_and_does_not_leak_notes():
    state = snapshot()
    feedback(state, scope="card")
    result = view(state)
    assert len(result["visit"]["observed_information"]) == 1
    assert "PRIVATE_FEEDBACK" not in json.dumps(result)


def test_private_notes_provenance_raw_prose_and_model_drafts_never_rendered():
    state = snapshot()
    sentinel = "SECRET_PRIVATE_SENTINEL"
    state["preferences"]["notes"] = sentinel
    state["sources"][0]["reason"] = sentinel
    state["process"]["run_id"] = sentinel
    state["limitations"] = [sentinel]
    for card in state["ledger"]:
        for k in ("observed", "inferred", "title"): card[k] = sentinel
        card["limitations"] = [sentinel]
        if card["suggestion"]:
            card["suggestion"].update(text=sentinel, reason=sentinel)
    for row in state["observations"]:
        row["provenance"].update(record_id=sentinel, adapter_version=sentinel)
        if row["purchase"]: row["purchase"]["merchant"] = sentinel
    result = view(state)
    assert sentinel not in json.dumps(result)
    for forbidden in ("demo-purchase-1", "demo-steps-", "purchase_recurrence", "recorded_steps", "demo_fixture", "Synthetic Cafe"):
        assert forbidden not in result["visit"]["brief"]
    assert len(result["visit"]["observed_information"]) == 2


@pytest.mark.parametrize("when", ["2026-11-31", "2026-11-14\nSECRET", "2027-01-01"])
def test_invalid_window_or_injected_dates_never_rendered(when):
    state = snapshot()
    state["ledger"][0]["window"]["end_date"] = when
    result = view(state)
    assert len(result["visit"]["observed_information"]) == 1
    assert "SECRET" not in result["visit"]["brief"]


def test_caption_and_home_remain_short_without_copying_multisentence_private_context():
    state = snapshot()
    state["preferences"]["notes"] = "One. Two. Three.\n" * 200
    result = view(state)
    assert len(result["visit"]["caption"]) < 240
    assert sum(result["visit"]["caption"].count(c) for c in ".!?") <= 2
    assert len(result["home"]["headline"]) < 180 and len(result["home"]["detail"]) < 240
    assert "One." not in json.dumps(result)


def test_fixture_backed_projection_and_copy_preserve_demo_marker():
    result = view()
    assert result["is_demo"] and result["provenance"] == "synthetic"
    assert result["visit"]["brief"].startswith(DEMO_LABEL)
    assert all(line.startswith("DEMO records") for line in result["visit"]["observed_information"])
    assert result["home"]["sample_label"]


def test_operational_settings_are_read_from_frozen_contract_not_clinical_thresholds():
    assert RULES["rule_version"] == "1.0.0"
    assert RULES["purchase_recurrence"]["minimum_distinct_dates"] == 3
    assert RULES["purchase_recurrence"]["minimum_span_days"] == 14
    assert RULES["purchase_recurrence"]["window_days_including_today"] == 28
    assert "not a clinical risk" in view()["visit"]["brief"]


def test_automatic_inference_requires_neither_suggestion_nor_confirmation():
    state = snapshot()
    for row in state["observations"][:3]: row["household_attribution"] = "unknown"
    state["ledger"][0]["suggestion"] = None
    result = view(state)
    assert result["home"]["headline"] == "Your recent purchases suggest you may be eating out often."
    assert result["clarification"]["should_prompt"]
    assert "2026-10-22 through 2026-11-10" in result["clarification"]["scope"]
    assert "not whether you ate any meals" in result["clarification"]["scope"]
    assert "no answer or typing is required" in result["visit"]["brief"]
    assert result["visit"]["confirmed_context"] == []
    assert result["clarification"]["key"] not in result["visit"]["brief"]


def test_yes_confirms_attribution_only_never_habit_or_consumption():
    state = snapshot()
    before = view(state)
    after = view(state, answers=response(state))
    assert after["visit"]["inferred_habits"] == before["visit"]["inferred_habits"]
    assert "does not confirm consumption" in after["visit"]["confirmed_context"][0]
    assert not after["clarification"]["should_prompt"]


def test_no_removes_personal_inference_without_erasing_observations_or_all_clear():
    state = snapshot()
    before = view(state)
    after = view(state, answers=response(state, "no"))
    assert after["visit"]["inferred_habits"] == []
    assert after["visit"]["observed_information"] == before["visit"]["observed_information"]
    assert after["home"]["status"] == "corrected"
    assert after["concern"]["status"] == "corrected"


@pytest.mark.parametrize("answer", ["not_sure", "dismissed"])
def test_uncertainty_and_dismissal_persist_without_reprompting(answer):
    state = snapshot()
    for _ in range(3):
        result = view(state, answers=response(state, answer))
        assert result["visit"]["inferred_habits"]
        assert not result["clarification"]["should_prompt"]
        assert "may not be personal" in result["visit"]["brief"]
        assert bool(result["visit"]["confirmed_context"]) == (answer == "not_sure")


def test_key_stable_across_ordering_notes_snapshot_and_card_revisions():
    state = snapshot()
    original = view(state)["clarification"]["key"]
    state["observations"].reverse()
    state["ledger"][0]["evidence"].reverse()
    state["ledger"][0]["revision"] += 1  # Deliberate fix versus old client key churn.
    state["revision"] += 1
    state["preferences"]["notes"] = "PRIVATE_UNRELATED_NOTE"
    assert view(state)["clarification"]["key"] == original
    assert state["data_epoch"] not in original and "demo-purchase-1" not in original


@pytest.mark.parametrize("change", ["epoch", "revision"])
def test_epoch_and_evidence_revision_invalidate_old_answers(change):
    state = snapshot()
    old = response(state, "no")
    if change == "epoch": state["data_epoch"] = "new-epoch"
    else:
        state["observations"][0]["revision"] += 1
        state["observations"][0]["supersedes"] = deepcopy(state["ledger"][0]["evidence"][0])
        state["ledger"][0]["evidence"][0]["revision"] += 1
    result = view(state, answers=old)
    assert result["clarification"]["key"] != old[0]["key"]
    assert result["clarification"]["answer"] is None
    assert result["visit"]["inferred_habits"]


@pytest.mark.parametrize("attribution", ["shared", "other"])
def test_explicit_attribution_correction_overrides_original_and_old_yes(attribution):
    state = snapshot()
    old = response(state)
    feedback(state, "partial", attribution)
    result = view(state, answers=old)
    assert result["clarification"] is None
    assert result["visit"]["inferred_habits"] == []
    assert result["visit"]["confirmed_context"] == []
    assert len(result["visit"]["observed_information"]) == 2


def test_latest_explicit_feedback_restores_corrected_evidence_not_historical_dispute():
    state = snapshot()
    feedback(state)
    assert view(state)["clarification"] is None
    feedback(state, "correct")
    assert view(state)["clarification"] is not None
    feedback(state, "partial", scope="card")
    assert view(state)["clarification"] is None
    feedback(state, "correct", scope="card")
    assert view(state)["clarification"] is not None


def test_partial_without_attribution_abstains_unknown_correction_is_qualified():
    state = snapshot()
    feedback(state, "partial")
    assert view(state)["clarification"] is None
    feedback(state, "partial", "unknown")
    assert view(state)["clarification"] is not None
    assert view(state)["visit"]["confirmed_context"] == []


def test_mixed_attribution_counts_only_supported_subset():
    state = snapshot()
    extra = deepcopy(state["observations"][0])
    extra.update(id="shared-extra", local_date="2026-11-12", household_attribution="shared")
    state["observations"].append(extra)
    state["ledger"][0]["evidence"].append(ref(extra))
    state["ledger"][0].update(purchase_count=4, distinct_purchase_dates=4, suggestion=None)
    result = view(state)
    assert "4 restaurant-category" in result["visit"]["observed_information"][0]
    assert "3 qualifying" in result["visit"]["inferred_habits"][0]
    assert "2026-11-12" not in result["clarification"]["scope"]
    assert result["concern"]["evidence_count"] == 3


@pytest.mark.parametrize("change", ["sparse", "short"])
def test_sparse_or_short_span_cannot_trigger_from_raw_prose(change):
    state = snapshot()
    state["ledger"][0]["inferred"] = "An alleged daily habit"
    if change == "sparse":
        state["ledger"][0]["evidence"].pop()
        state["ledger"][0].update(purchase_count=2, distinct_purchase_dates=2)
    else:
        for i in range(3): state["observations"][i]["local_date"] = f"2026-11-1{i}"
    result = view(state)
    assert not result["visit"]["inferred_habits"]
    assert result["clarification"] is None
    assert "alleged" not in result["visit"]["brief"]


@pytest.mark.parametrize("change", ["disabled", "suppressed", "unavailable", "scenario"])
def test_suppressed_disabled_unavailable_do_not_create_concern_or_question(change):
    state = snapshot()
    if change == "disabled": state["preferences"]["food_suggestions"] = False
    elif change == "scenario": state["preferences"]["suppressed_scenario_ids"] = ["purchase_recurrence"]
    else: state["ledger"][0]["suggestion"]["status"] = change
    result = view(state)
    assert not result["visit"]["inferred_habits"]
    assert result["clarification"] is None
    assert result["home"]["status"] != "no_flags"


def test_not_useful_prevents_question_even_without_suggestion():
    state = snapshot()
    state["ledger"][0]["suggestion"] = None
    feedback(state, "not_useful", kind="usefulness", scope="card")
    assert view(state)["clarification"] is None
    feedback(state, "useful", kind="usefulness", scope="card")
    assert view(state)["clarification"] is not None


@pytest.mark.parametrize("change", ["mode", "source", "rule", "timezone"])
def test_mixed_modes_sources_rules_and_timezones_cannot_create_habit(change):
    state = snapshot()
    if change == "mode": state["observations"][0]["mode"] = "live"
    elif change == "source": state["observations"][0]["source_id"] = "plaid"
    elif change == "rule": state["ledger"][0]["rule_version"] = "future"
    else: state["ledger"][0]["window"]["timezone"] = "UTC"
    assert view(state)["clarification"] is None


@pytest.mark.parametrize("answer", ["yes", "no", "not_sure", "dismissed"])
def test_every_tap_preserves_short_caption_demo_marker_and_safe_sections(answer):
    state = snapshot()
    result = view(state, answers=response(state, answer))
    assert len(result["visit"]["caption"]) < 240
    assert result["visit"]["brief"].startswith(DEMO_LABEL)
    for heading in ("Observed information", "Inferred habit — unconfirmed", "User-reported context — optional"):
        assert heading in result["visit"]["brief"]
    assert not result["clarification"]["should_prompt"]
    assert bool(result["visit"]["inferred_habits"]) == (answer != "no")


def test_deterministic_read_only_and_contract_example_valid():
    state = snapshot()
    before = deepcopy(state)
    assert view(state) == view(state)
    assert state == before
    assert "Nothing has been sent to a doctor" in view(state)["visit"]["brief"]
    Draft202012Validator.check_schema(EXPERIENCE_SCHEMA)
    for example in EXPERIENCE_SCHEMA["examples"]:
        Draft202012Validator(EXPERIENCE_SCHEMA, format_checker=FormatChecker()).validate(example)


def test_activity_selected_source_and_healthkit_read_access_must_be_verified():
    state = snapshot()
    for row in state["observations"][3:]: row["source_id"] = "healthkit"
    source = deepcopy(state["sources"][0]); source.update(id="healthkit", read_access="unknown")
    state["sources"][1] = source
    state["preferences"]["enabled_source_ids"].append("healthkit")
    assert len(view(state)["visit"]["observed_information"]) == 1
    source["read_access"] = "samples_readable"
    assert len(view(state)["visit"]["observed_information"]) == 2
    assert len(view(state, selected_sources={"recorded_steps": "demo"})["visit"]["observed_information"]) == 1


def test_shifted_window_with_same_evidence_does_not_reset_question():
    state = snapshot()
    old = response(state, "dismissed")
    state["evaluation_date"] = "2026-11-15"
    state["ledger"][0]["window"].update(start_date="2026-10-19", end_date="2026-11-15")
    result = view(state, answers=old, now=iso(NOW + timedelta(days=1)), as_of_date="2026-11-15")
    assert result["clarification"]["key"] == old[0]["key"]
    assert not result["clarification"]["should_prompt"]


def test_new_material_evidence_cooldown_does_not_reuse_old_answer():
    state = snapshot()
    old = response(state, "no")
    state["observations"][0]["revision"] += 1
    state["ledger"][0]["evidence"][0]["revision"] += 1
    # Historical predecessor retained, as in the durable immutable domain ledger.
    state["observations"].append(deepcopy(EXAMPLE["observations"][0]))
    result = view(state, answers=old)
    assert result["clarification"]["answer"] is None
    assert not result["clarification"]["should_prompt"]
    assert result["clarification"]["next_prompt_at"] == iso(NOW + timedelta(days=7))
    assert result["visit"]["inferred_habits"]
    after = view(state, answers=old, now=iso(NOW + timedelta(days=7)))
    assert after["clarification"]["should_prompt"]
