"""Authoritative deterministic experience. No network, persistence, HTML or clinical scoring."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json

from ..contracts import DomainError, RULES
from ..domain import instant, iso, key, ref, refkey, zone

DEMO_LABEL = "DEMO — fictional example, not your health information"
STARTING_POINT = "For a nonurgent wellbeing conversation, primary care or family medicine is an optional starting point."
SCOPE_NOTICE = "Preparation for a conversation, not a diagnosis or symptom assessment. This feature does not assess urgent symptoms."
BASE_LIMITS = [
    "Coverage is partial or unknown; records are not a complete account of eating or activity.",
    "Purchases do not verify meals, consumption, portions, diet quality, or personal health risk; a likely routine remains an inference.",
    "Missing activity records are unknown, not zero; recorded steps do not establish total activity or inactivity.",
    "Operational purchase patterns are not clinical concerns or disease probabilities.",
    "This record review is not a health assessment or an all-clear.",
]
OPTIONS = [{"value": value, "label": label} for value, label in
           (("yes", "Yes"), ("no", "No"), ("not_sure", "Not sure"), ("dismissed", "Skip this question"))]
SUGGESTIONS = {
    "purchase_recurrence": "For one upcoming food purchase, consider planning an option that fits your budget and preferences ahead of time.",
    "recorded_steps": "If it is comfortable and fits your usual activity, consider five minutes of movement you enjoy after an existing daily routine.",
}


def parsed_date(value):
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except (ValueError, TypeError):
        return None


def latest_feedback(state, card, kind, scope, observation=None):
    matches = [f for f in state["feedback"] if f["card_id"] == card["id"] and f["kind"] == kind and f["scope"] == scope
               and (scope != "event" or f["event_ref"] == ref(observation))]
    return matches[-1] if matches else None


def disputed(feedback):
    return feedback is not None and (feedback["value"] == "incorrect" or
                                     feedback["value"] == "partial" and feedback["attribution"] is None)


def can_suggest(card, preferences):
    suggestion = card.get("suggestion")
    if not suggestion or suggestion["status"] != "eligible" or not suggestion["text"].strip() or card["status"] != "observed":
        return False
    if card["scenario_id"] in preferences["suppressed_scenario_ids"]:
        return False
    if any(suggestion[requires] and preferences[setting] for requires, setting in
           (("requires_kitchen", "no_kitchen"), ("requires_spending", "no_spending"), ("requires_movement", "movement_restriction"))):
        return False
    return preferences["food_suggestions"] if card["scenario_id"] == "purchase_recurrence" else preferences["activity_suggestions"] and not preferences["movement_restriction"]


def verified_summary(card, state, selected_sources):
    start, end, evaluation = (parsed_date(v) for v in (card["window"]["start_date"], card["window"]["end_date"], state["evaluation_date"]))
    refs = card["evidence"]
    if card["status"] != "observed" or not refs or not all((start, end, evaluation)) or not start <= end <= evaluation:
        return None
    if card["window"]["timezone"] != state["preferences"]["timezone"] or len({r["observation_id"] for r in refs}) != len(refs):
        return None
    try:
        zone(card["window"]["timezone"])
    except DomainError:
        return None
    verdict = latest_feedback(state, card, "correctness", "card")
    if verdict and verdict["value"] != "correct":
        return None
    rows = []
    for reference in refs:
        matching = [o for o in state["observations"] if key(o) == refkey(reference)]
        if len(matching) != 1:
            return None
        o = matching[0]
        local = parsed_date(o["local_date"])
        if o["operation"] != "upsert" or o["mode"] != card["mode"] or o["timezone"] != card["window"]["timezone"] or not local or not start <= local <= end:
            return None
        if any((v["id"] == o["id"] and v["revision"] > o["revision"]) or v["supersedes"] == reference for v in state["observations"]):
            return None
        sources = [s for s in state["sources"] if s["id"] == o["source_id"]]
        if len(sources) != 1:
            return None
        source = sources[0]
        if not source["enabled"] or source["consent"] != "granted" or source["capability"] != "available" or source["status"] != "ready" or source["id"] not in state["preferences"]["enabled_source_ids"]:
            return None
        if source["id"] == "healthkit" and source["read_access"] != "samples_readable":
            return None
        if source["last_success_at"]:
            try:
                if instant(source["last_success_at"]).astimezone(zone(state["preferences"]["timezone"])).date() < evaluation - timedelta(days=1):
                    return None
            except (ValueError, TypeError):
                return None
        if selected_sources.get(card["scenario_id"]) and source["id"] != selected_sources[card["scenario_id"]]:
            return None
        rows.append(o)
    if len({o["source_id"] for o in rows}) != 1:
        return None
    prefix = "Plaid Sandbox records" if rows[0]["source_id"] == "plaid" else "DEMO records" if card["mode"] == "fixture" else "Connected records"
    period = f"{start} through {end}"
    if card["scenario_id"] == "purchase_recurrence":
        if not all(o["kind"] == "purchase" and o["lifecycle"] == "posted" and o["purchase"] and not o["purchase"]["is_refund"] and o["purchase"]["category"] == "restaurant" for o in rows):
            return None
        days = len({o["local_date"] for o in rows})
        if card["purchase_count"] != len(rows) or card["distinct_purchase_dates"] != days:
            return None
        summary = f"{prefix}, {period}: {len(rows)} restaurant-category purchase records on {days} dates, not a count of meals or evidence of who ate them."
        if any(disputed(latest_feedback(state, card, "correctness", "event", o)) for o in rows):
            summary += " Some transaction context is disputed and excluded from habit inference."
    else:
        if card["scenario_id"] != "recorded_steps" or len({o["local_date"] for o in rows}) != len(rows):
            return None
        if not all(o["kind"] == "activity" and o["lifecycle"] == "recorded" and o["activity"] and o["activity"]["metric"] == "steps" and o["activity"]["unit"] == "count" and type(o["activity"]["value"]) is int and o["activity"]["value"] >= 0 for o in rows):
            return None
        total = sum(o["activity"]["value"] for o in rows)
        if total > 2**63 - 1 or total != card["recorded_steps"] or len(rows) != card["days_with_records"]:
            return None
        if any((f := latest_feedback(state, card, "correctness", "event", o)) and f["value"] != "correct" for o in rows):
            return None
        summary = f"{prefix}, {period}: {total} recorded steps across {len(rows)} dates with records; coverage within those dates is unknown, and this is not total activity."
    return {"card": card, "rows": rows, "summary": summary}


def clean_answers(state, answers, now):
    sources = {s["id"] for s in state["sources"] if s["enabled"] and s["consent"] == "granted"}
    evidence = {key(o) for o in state["observations"]}
    kept = []
    for answer in answers:
        try:
            if answer["data_epoch"] == state["data_epoch"] and set(answer["source_ids"]) <= sources and all(refkey(r) in evidence for r in answer["evidence"]) and now - instant(answer["created_at"]) <= timedelta(days=90):
                kept.append(deepcopy(answer))
        except (KeyError, ValueError, TypeError):
            continue
    return kept[-128:]


def purchase_pattern(review, state):
    card, rows, preferences = review["card"], review["rows"], state["preferences"]
    limits = []
    useful = latest_feedback(state, card, "usefulness", "card")
    if useful and useful["value"] != "useful":
        return {"blocked": True, "limits": ["You marked this pattern as not useful or only partly useful; no new personal interpretation or question is offered."]}
    if not preferences["food_suggestions"] or card.get("suggestion") and card["suggestion"]["status"] in {"suppressed", "unavailable"}:
        return {"blocked": True, "limits": ["Food-pattern presentation is disabled or unavailable; this is not an all-clear."]}
    rule = RULES["purchase_recurrence"]
    start, end = parsed_date(card["window"]["start_date"]), parsed_date(card["window"]["end_date"])
    if card["rule_version"] != RULES["rule_version"] or card["window"]["end_date"] != state["evaluation_date"] or (end-start).days != rule["window_days_including_today"]-1:
        return {"blocked": True, "limits": ["This purchase window or rule version is not supported for habit inference."]}
    usable, identity = [], ["experience-attribution-v1", state["data_epoch"], card["scenario_id"], card["rule_version"], card["mode"], card["window"]["timezone"]]
    for o in sorted(rows, key=key):
        feedback = latest_feedback(state, card, "correctness", "event", o)
        attribution = feedback["attribution"] if feedback and feedback["attribution"] is not None else o["household_attribution"]
        identity += [o["id"], o["revision"], o["source_id"], o["local_date"], attribution, feedback["value"] if feedback else ""]
        if not disputed(feedback) and attribution in {"self", "unknown"}:
            usable.append(o)
    if len(usable) != len(rows):
        limits.append("Shared, other-attributed, or disputed records are excluded from the personal pattern; observed totals may be larger.")
    dates = sorted({o["local_date"] for o in usable})
    span = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days if dates else 0
    recurrent = len(dates) >= rule["minimum_distinct_dates"] and span >= rule["minimum_span_days"]
    fingerprint = "purchase-attribution:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return {"blocked": False, "limits": limits, "recurrent": recurrent, "unambiguous": len(usable) == len(rows),
            "rows": usable, "dates": dates, "span": span, "key": fingerprint,
            "scope": {"key": fingerprint, "evidence": [ref(o) for o in rows], "source_ids": sorted({o["source_id"] for o in rows})}}


def action(kind, label, detail=None, card_id=None, suggestion_id=None):
    return {"kind": kind, "label": label, "detail": detail, "card_id": card_id, "suggestion_id": suggestion_id}


def project(payload):
    state = payload["state"]
    now = instant(payload["now"])
    preferences = state["preferences"]
    answers = clean_answers(state, payload.get("answers", []), now)
    modes = {o["mode"] for o in state["observations"]}
    provenance = "mixed" if len(modes)>1 else "synthetic" if modes == {"fixture"} else "live" if modes == {"live"} else "none"
    is_demo = "fixture" in modes
    sandbox = any(o["source_id"] == "plaid" for o in state["observations"])
    sample_label = "Plaid Sandbox — synthetic bank records, not live finances" if sandbox else ("Sample data — synthetic records" if is_demo else None)
    limits, reviews, hidden = list(BASE_LIMITS), {}, False
    process = state["process"]
    ready = payload.get("review_ready", process["status"] == "completed" and process["ledger_status"] == "completed")
    ready = ready and process["ledger_status"] != "failed"
    current = parsed_date(payload["as_of_date"]) is not None and state["evaluation_date"] == payload["as_of_date"]
    if ready and current:
        for sid in ("purchase_recurrence", "recorded_steps"):
            cards = [c for c in state["ledger"] if c["scenario_id"] == sid]
            if len(cards) != 1:
                limits.append("A record category is missing or ambiguous and was not reviewed.")
                continue
            card = cards[0]
            suppressed = sid in preferences["suppressed_scenario_ids"] or card["status"] == "suppressed"
            candidate_hidden = card.get("suggestion") and card["suggestion"]["status"] in {"suppressed", "unavailable"}
            disabled = not preferences["food_suggestions"] if sid == "purchase_recurrence" else not preferences["activity_suggestions"] or preferences["movement_restriction"]
            if suppressed or candidate_hidden or disabled:
                hidden = True
                limits.append("Some information or suggestions are hidden by preferences; hidden items do not mean there are no concerns.")
            if suppressed:
                continue
            reviewed = verified_summary(card, state, payload.get("selected_sources", {}))
            if reviewed:
                reviews[sid] = reviewed
            else:
                limits.append("Some records are unavailable, incomplete, disputed, or out of date and were not summarized.")
    else:
        limits.append("Current records were not reviewed because data is missing, pending, conflicted, or not current for this date.")
    pattern = purchase_pattern(reviews["purchase_recurrence"], state) if "purchase_recurrence" in reviews else None
    if pattern:
        limits += pattern["limits"]
        hidden = hidden or pattern["blocked"]
    habits, confirmed, clarification, question_scope = [], [], None, None
    no_answer = False
    concern = {"status": "not_reviewed", "title": "Not enough information yet", "summary": "No current pattern review is available.",
               "evidence_count": None, "distinct_dates": None, "span_days": None, "first_date": None, "last_date": None}
    if pattern and not pattern["blocked"]:
        dates = pattern["dates"]
        concern.update(evidence_count=len(pattern["rows"]), distinct_dates=len(dates), span_days=pattern["span"],
                       first_date=dates[0] if dates else None, last_date=dates[-1] if dates else None)
        if pattern["recurrent"]:
            question_scope = pattern["scope"]
            answer = next((a["answer"] for a in reversed(answers) if a["key"] == pattern["key"]), None)
            latest = max((instant(a["created_at"]) for a in answers), default=None)
            due = latest + timedelta(days=7) if latest else None
            clarification = {"key": pattern["key"], "question": "Were these purchases mostly for you?",
                "scope": f"The {len(pattern['rows'])} qualifying restaurant-category purchase records on {len(dates)} dates, {dates[0]} through {dates[-1]}. This asks who the purchases were for, not whether you ate any meals.",
                "answer": answer, "should_prompt": answer is None and (due is None or now >= due), "can_answer": True,
                "next_prompt_at": iso(due) if answer is None and due and now < due else None, "options": deepcopy(OPTIONS)}
            limits.append("The existing product recurrence rule supports a possible routine, not a clinical risk, nutritional assessment, or medical threshold.")
            period = f"{dates[0]} through {dates[-1]}"
            if answer == "no":
                no_answer = True
                confirmed.append(f"Tap-confirmed context for {period}: these purchases were not mostly for me. The personal habit inference has been removed.")
                concern.update(status="corrected", title="Your correction is saved", summary="These purchases were not mostly for you.")
            else:
                if answer == "yes":
                    confirmed.append(f"Tap-confirmed context for {period}: these purchases were mostly for me. This does not confirm consumption or diet quality.")
                elif answer == "not_sure":
                    confirmed.append(f"Tap-reported context for {period}: not sure who these purchases were mostly for; attribution remains uncertain.")
                habits = [f"Based on {len(pattern['rows'])} qualifying restaurant-category purchase records on {len(dates)} dates, {period}, eating out or takeaway may be a recurring routine. This is unconfirmed; purchases do not establish personal consumption or diet quality."]
                concern.update(status="unconfirmed_pattern", title="A possible eating-out routine", summary="Your recent purchases suggest you may be eating out often.")
                if answer != "yes":
                    limits.append("Who these purchases were mostly for is unconfirmed; the possible routine may not be personal.")
    if any(o["household_attribution"] != "self" for o in state["observations"]):
        limits.append("Shared or other-attributed purchases do not support the personal inference; unknown attribution remains uncertain, not confirmed personal eating or activity.")
    no_flags = bool(pattern and not pattern["blocked"] and not pattern["recurrent"] and pattern["unambiguous"] and len(reviews) == 2 and not hidden)
    if no_flags:
        concern.update(status="no_pattern", title="No patterns flagged in available records", summary="No recurring purchase pattern was flagged in the records reviewed.")
    stale = not current or any(s["enabled"] and s["consent"] == "granted" and s["status"] == "stale" for s in state["sources"])
    home_status = "pattern" if habits else "corrected" if no_answer else "no_flags" if no_flags else "stale" if stale else "suppressed" if hidden else "review_pending" if not ready and state["observations"] else "insufficient"
    visit_status = "inferred_pattern" if habits else "corrected" if no_answer else "no_patterns_flagged" if no_flags else "insufficient_information"
    title = concern["title"] if habits or no_answer or no_flags else "Not enough information yet"
    caption = concern["summary"] if habits or no_answer or no_flags else "Available records do not support a personal routine inference."
    reason = "An automatically inferred, unconfirmed eating-out routine may be worth discussing if it matters to you; it is not a user-reported concern or a medical finding." if habits else "An optional general wellbeing conversation can use the available records without assuming a personal habit."
    questions = ["Does this possible routine fit my circumstances?", "If I want to change it, what options fit my preferences?"] if habits else ["What context would help us discuss my wellbeing?", "Is any follow-up useful, and what should I bring next time?"]
    preferences_copy = []
    if habits:
        if preferences["no_kitchen"]:
            preferences_copy.append("User-reported preference: no kitchen available.")
        if preferences["no_spending"]:
            preferences_copy.append("User-reported preference: no-spending options.")
    observed = [reviews[sid]["summary"] for sid in ("purchase_recurrence", "recorded_steps") if sid in reviews]
    visit = {"status": visit_status, "record_review": "limited" if observed else "not_reviewed", "title": title, "caption": caption,
             "reason_for_discussion": reason, "doctor_starting_point": STARTING_POINT, "scope_notice": SCOPE_NOTICE,
             "observed_information": observed, "inferred_habits": habits, "confirmed_context": confirmed,
             "user_reported_preferences": preferences_copy, "uncertainties": list(dict.fromkeys(limits)), "questions": questions, "brief": ""}
    sections = [sample_label if sandbox else "DEMO — includes synthetic sample records alongside connected records" if provenance == "mixed" else DEMO_LABEL] if is_demo else []
    sections += ["Visit preparation\n" + SCOPE_NOTICE, "Reason for discussion\n" + reason, "Starting point\n" + STARTING_POINT]
    for heading, lines, empty in (
        ("Observed information — " + ("records partially reviewed" if observed else "records not reviewed"), observed, "No current, verified record summary is included."),
        ("Inferred habit — unconfirmed", habits, "No personal habit inference is included."),
        ("User-reported context — optional", confirmed, "No eating or activity context has been confirmed; no answer or typing is required."),
        ("User-reported preferences", preferences_copy, "No relevant explicit preferences are included."),
        ("Uncertainties", visit["uncertainties"], ""), ("Questions to ask", questions, "")):
        sections.append(heading + "\n" + "\n".join("- " + line for line in (lines or [empty])))
    sections.append("Review before copying; editing is optional. Nothing has been sent to a doctor.")
    visit["brief"] = "\n\n".join(sections)
    home = {"status": home_status, "headline": title, "detail": caption, "info": visit["uncertainties"], "sample_label": sample_label, "primary_action": None}
    if habits:
        home.update(headline="Your recent purchases suggest you may be eating out often.", detail="A possible routine, not a confirmed eating history.", primary_action=action("view_visit", "Prepare for a conversation"))
    elif no_answer:
        home.update(headline="Your correction is saved.", detail="The personal inference was removed; recorded purchases are unchanged.", primary_action=action("view_visit", "Review your brief"))
    elif no_flags:
        home.update(headline="No patterns flagged in available records", detail="This is limited to the purchase and step records reviewed.", primary_action=action("view_ledger", "Review inference history"))
    elif reviews:
        candidates = sorted(reviews.values(), key=lambda r: (not can_suggest(r["card"], preferences), r["card"]["scenario_id"] != "recorded_steps"))
        card = candidates[0]["card"]
        if card["scenario_id"] == "recorded_steps":
            home.update(headline=f"Your records include {card['recorded_steps']:,} steps across {card['days_with_records']} {'day' if card['days_with_records']==1 else 'days'}.", detail="Recorded steps do not establish total activity.")
        else:
            home.update(headline=f"Your records include {card['purchase_count']} purchases at merchants categorized as restaurants.", detail="Purchase records do not establish meals or who ate them.")
        if can_suggest(card, preferences):
            home["primary_action"] = action("explore_suggestion", "Explore this idea", SUGGESTIONS[card["scenario_id"]], card["id"], card["suggestion"]["id"])
        else:
            home["primary_action"] = action("view_ledger", "Review inference history")
    else:
        home.update(headline="Records need an update" if stale else "Some patterns are hidden" if hidden else "Not enough information yet",
                    detail="Review your connections or preferences when you are ready.", primary_action=action("connect", "Review connections"))
    public = {k: deepcopy(state[k]) for k in ("data_epoch", "revision", "generated_at", "evaluation_date")}
    public.update(schema_version="1", timezone=preferences["timezone"], is_demo=is_demo, provenance=provenance,
                  home=home, concern=concern, clarification=clarification, visit=visit,
                  inference_history=deepcopy(payload.get("inference_history", [])))
    return {"experience": public, "question_scope": question_scope, "answers": answers,
            "inference_evidence": [ref(o) for o in pattern["rows"]] if pattern and not pattern["blocked"] else []}


class ExperienceExtension:
    id, version, status = "experience", "1", "available"

    async def handle(self, operation, payload):
        if operation == "clean":
            answers = clean_answers(payload["state"], payload.get("answers", []), instant(payload["now"]))
            return {"answers": answers, "removed": len(answers) != len(payload.get("answers", []))}
        result = project(payload)
        if operation == "project":
            return result
        if operation != "answer":
            raise ValueError("Unsupported experience operation")
        request = payload["request"]
        question = result["experience"]["clarification"]
        if question is None or question["key"] != request["clarification_key"] or not question["can_answer"]:
            raise DomainError("obsolete_pattern", 409, "The records behind this question changed. Refresh experience before answering.")
        if request["answer"] not in {o["value"] for o in OPTIONS}:
            raise DomainError()
        answers = [a for a in result["answers"] if a["key"] != question["key"]]
        answers.append({**result["question_scope"], "answer": request["answer"], "created_at": payload["now"], "data_epoch": payload["state"]["data_epoch"]})
        return {"answers": answers[-128:]}
