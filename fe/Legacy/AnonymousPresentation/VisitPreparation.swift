import Foundation
import CryptoKit

/// Only explicit, user-confirmed inputs belong here. Do not prefill from purchases,
/// model prose, private notes, or an inferred health assessment.
public struct VisitConfirmedContext: Equatable, Sendable {
    public enum Concern: String, CaseIterable, Sendable {
        case notReviewed, noneReported, eatingRoutine, activityRoutine, generalWellbeing
    }

    public var confirmedConcern: Concern
    public var confirmedEatingContext: String
    public var confirmedActivityContext: String

    public init(
        confirmedConcern: Concern = .notReviewed,
        confirmedEatingContext: String = "",
        confirmedActivityContext: String = ""
    ) {
        self.confirmedConcern = confirmedConcern
        self.confirmedEatingContext = confirmedEatingContext
        self.confirmedActivityContext = confirmedActivityContext
    }
}

/// An optional tap answer, scoped to a specific epoch and evidence version. The
/// host persists this value locally; it is never a correction to the frozen ledger.
public struct VisitClarificationResponse: Codable, Equatable, Sendable {
    public enum Answer: String, Codable, CaseIterable, Sendable {
        case yes, no, notSure, dismissed
    }
    public let key: String
    public let answer: Answer

    public init(key: String, answer: Answer) {
        self.key = key
        self.answer = answer
    }
}

public struct VisitClarification: Equatable, Sendable {
    /// Opaque deterministic key for local persistence; never included in the brief.
    public let key: String
    public let question: String
    public let scope: String
    public let answer: VisitClarificationResponse.Answer?
    public var shouldPrompt: Bool { answer == nil }
}

/// A local, deterministic visit-preparation projection, never a clinical assessment.
/// Core has no network, persistence, clipboard, or clock dependency.
public struct VisitPreparation: Equatable, Sendable {
    public enum Status: Sendable {
        case userReportedConcern, inferredPattern, noConcernsFlagged, insufficientInformation
    }
    public enum RecordReview: Sendable {
        case limited, notReviewed
    }
    public enum Demo: String, CaseIterable, Sendable {
        case eatingRoutine, noFlags, insufficientData
    }

    public let status: Status
    public let recordReview: RecordReview
    public let title: String
    /// Fixed copy, never interpolated user prose: at most two sentences.
    public let caption: String
    public let isDemo: Bool
    public let reasonForDiscussion: String
    public let observedInformation: [String]
    /// Probabilistic presentation-layer interpretation, never a verified diet fact.
    public let inferredHabits: [String]
    public let clarification: VisitClarification?
    public let userReportedContext: [String]
    public let userReportedPreferences: [String]
    public let uncertainties: [String]
    public let questions: [String]

    public static let demoLabel = "DEMO — fictional example, not your health information"
    public static let doctorStartingPoint = "If you would like a nonurgent general wellbeing discussion, primary care or family medicine is an optional starting point. A purchase pattern alone does not establish a need for medical care or a specialist."
    public static let scopeNotice = "Preparation for a conversation, not a diagnosis or symptom assessment. This feature does not assess urgent symptoms."

    public var recordReviewLabel: String {
        recordReview == .limited ? "Records partially reviewed" : "Records not reviewed"
    }

    /// Plain editable copy. Never interpolates ledger prose, inferred text, merchant
    /// names, source reasons, provider fields, feedback notes, or technical IDs.
    public var brief: String {
        var sections = [String]()
        if isDemo { sections.append(Self.demoLabel) }
        sections += [
            "Visit preparation\n\(Self.scopeNotice)",
            "Reason for discussion\n\(reasonForDiscussion)",
            "Starting point\n\(Self.doctorStartingPoint)",
            "Observed information — \(recordReviewLabel.lowercased())\n" + bullets(observedInformation.isEmpty
                ? ["No current, verified record summary is included."] : observedInformation),
            "Inferred habit — unconfirmed\n" + bullets(inferredHabits.isEmpty
                ? ["No personal habit inference is included."] : inferredHabits),
            "User-reported context — optional\n" + bullets(userReportedContext.isEmpty
                ? ["No eating or activity context has been confirmed; no answer or typing is required."] : userReportedContext),
            "User-reported preferences\n" + bullets(userReportedPreferences.isEmpty
                ? ["No relevant explicit preferences are included."] : userReportedPreferences),
            "Uncertainties\n" + bullets(uncertainties),
            "Questions to ask\n" + bullets(questions),
            "Review before copying; editing is optional. Nothing has been sent to a doctor."
        ]
        return sections.joined(separator: "\n\n")
    }

    /// Pass today's local calendar date explicitly (yyyy-MM-dd), in the State's
    /// evaluation timezone. Pending local changes/conflicts must come from the host.
    /// Current contracts have only partial/unknown coverage: an absence of reported
    /// concerns can NEVER turn this real-state projection into an all-clear result.
    public init(
        state: State?,
        context: VisitConfirmedContext = .init(),
        asOfLocalDate: String,
        hasPendingChanges: Bool = false,
        hasConflict: Bool = false,
        clarificationResponse: VisitClarificationResponse? = nil
    ) {
        let concern = context.confirmedConcern
        let reportedConcern = [.eatingRoutine, .activityRoutine, .generalWellbeing].contains(concern)
        var reported = [String]()
        if reportedConcern { reported.append("User-reported discussion topic: \(Self.topic(for: concern)).") }
        if concern == .noneReported { reported.append("User reports no concern to discuss at this time; this is not a health assessment.") }
        for (label, input) in [("Eating context", context.confirmedEatingContext), ("Activity context", context.confirmedActivityContext)] {
            let text = Self.singleLine(input)
            if !text.isEmpty { reported.append("User-reported \(label.lowercased()): \(text)") }
        }
        isDemo = state.map { snapshot in
            snapshot.observations.contains { $0.mode == "fixture" }
                || snapshot.ledger.contains { $0.mode == "fixture" }
        } ?? false

        var limits = [
            "Coverage is partial or unknown; records are not a complete account of eating or activity.",
            "Purchases do not verify meals, consumption, portions, diet quality, or personal health risk; a likely routine remains an inference.",
            "Missing activity records are unknown, not zero; recorded steps do not establish total activity or inactivity.",
            "Operational purchase patterns are not clinical concerns or disease probabilities."
        ]
        var observed = [String]()
        var habits = [String]()
        var question: VisitClarification?
        if let state, !hasPendingChanges, !hasConflict,
           Self.date(asOfLocalDate) != nil, state.evaluation_date == asOfLocalDate,
           state.process.status == "completed", state.process.ledger_status == "completed" {
            for scenario in ["purchase_recurrence", "recorded_steps"] {
                let cards = state.ledger.filter { $0.scenario_id == scenario }
                guard cards.count == 1, let card = cards.first else {
                    limits.append("A record category is missing or ambiguous and was not reviewed.")
                    continue
                }
                if state.preferences.suppressed_scenario_ids.contains(scenario)
                    || card.status == "suppressed" || card.suggestion?.status == "suppressed" {
                    limits.append("Some information or suggestions are hidden by preferences; hidden items do not mean there are no concerns.")
                    // A suppressed suggestion does not erase valid observations.
                    if card.status == "suppressed" || state.preferences.suppressed_scenario_ids.contains(scenario) { continue }
                }
                guard let summary = Self.summary(card: card, state: state) else {
                    limits.append("Some records are unavailable, incomplete, disputed, or out of date and were not summarized.")
                    continue
                }
                observed.append(summary)
                if scenario == "purchase_recurrence" {
                    let pattern = Self.inferPurchasePattern(card: card, state: state, response: clarificationResponse)
                    habits = pattern.habits
                    question = pattern.clarification
                    reported += pattern.reported
                    limits += pattern.limitations
                }
            }
        } else {
            limits.append("Current records were not reviewed because data is missing, pending, conflicted, or not current for this date.")
        }
        inferredHabits = habits
        clarification = question
        userReportedContext = reported
        // Existing explicit preferences are a separate provenance category, not
        // fabricated tap answers or an assumed eating history. Never copy notes.
        var preferencesCopy = [String]()
        if let preferences = state?.preferences {
            if !habits.isEmpty || concern == .eatingRoutine || !Self.singleLine(context.confirmedEatingContext).isEmpty {
                if preferences.no_kitchen { preferencesCopy.append("User-reported preference: no kitchen available.") }
                if preferences.no_spending { preferencesCopy.append("User-reported preference: no-spending options.") }
            }
            if concern == .activityRoutine || !Self.singleLine(context.confirmedActivityContext).isEmpty {
                if preferences.movement_restriction {
                    preferencesCopy.append("User-reported preference: avoid movement suggestions; this does not specify a medical condition.")
                }
            }
        }
        userReportedPreferences = preferencesCopy
        recordReview = observed.isEmpty ? .notReviewed : .limited
        observedInformation = observed
        if reportedConcern {
            status = .userReportedConcern
            title = "A concern to discuss"
            caption = "You reported a routine or wellbeing concern to discuss. Any pattern inferred from purchases remains unconfirmed."
            reasonForDiscussion = Self.reason(for: concern)
            questions = Self.questions(for: concern)
        } else if !habits.isEmpty && concern != .noneReported {
            status = .inferredPattern
            title = "A possible eating-out routine"
            caption = "Recurring restaurant-category purchases suggest eating out may be frequent. Who ate what and diet quality are not established by purchases."
            reasonForDiscussion = "An automatically inferred, unconfirmed eating-out routine may be worth discussing if it matters to you; it is not a user-reported concern or a medical finding."
            questions = ["Does this possible routine fit my circumstances?", "If I want to change it, what options fit my preferences?"]
        } else {
            status = .insufficientInformation
            title = "Not enough information yet"
            if question?.answer == .no {
                caption = "You said these purchases were not mostly for you, so the personal inference was removed. This is not an assessment of your health."
            } else if concern == .noneReported {
                caption = "You reported no concern to discuss. Incomplete records do not provide an all-clear."
            } else {
                caption = "Available records do not support a personal routine inference. Missing or unreviewed information is not an all-clear."
            }
            reasonForDiscussion = concern == .noneReported ? Self.reason(for: concern)
                : "An optional general wellbeing conversation can use the available records without assuming a personal habit."
            questions = Self.questions(for: concern)
        }
        if state?.observations.contains(where: { $0.household_attribution != "self" }) == true {
            limits.append("Shared or other-attributed purchases do not support the personal inference; unknown attribution remains uncertain, not confirmed personal eating or activity.")
        }
        // Stable de-duplication preserves deterministic ordering.
        uncertainties = limits.reduce(into: []) { if !$0.contains($1) { $0.append($1) } }
    }

    /// Presentation-only fixtures. These never create or mutate State or persist
    /// fictional context as user facts. Every display and copied draft stays labeled.
    public static func demo(_ example: Demo) -> Self {
        demo(example, clarificationResponse: nil)
    }

    public static func demo(_ example: Demo, clarificationResponse: VisitClarificationResponse?) -> Self {
        switch example {
        case .eatingRoutine:
            let key = "demo-visit-purchase-attribution-v1"
            let answer = clarificationResponse?.key == key ? clarificationResponse?.answer : nil
            let clarification = VisitClarification(key: key, question: "Were these purchases mostly for you?",
                scope: "Fictional recurring-purchase example only, with no real records or dates. This asks about purchase attribution, not consumption.", answer: answer)
            let reported: [String]
            switch answer {
            case .yes: reported = ["DEMO tap-confirmed context: purchases mostly for the fictional person; consumption remains unconfirmed."]
            case .no: reported = ["DEMO tap-confirmed correction: purchases not mostly for the fictional person; personal inference removed."]
            case .notSure: reported = ["DEMO tap-reported context: not sure who the purchases were for."]
            case .dismissed, nil: reported = []
            }
            return Self(
                status: answer == .no ? .insufficientInformation : .inferredPattern, recordReview: .notReviewed,
                title: answer == .no ? "Not enough information yet" : "A possible eating-out routine",
                caption: answer == .no
                    ? "The fictional person's No answer removes the personal inference. This is not a health assessment."
                    : "In this fictional example, recurring restaurant purchases suggest eating out may be frequent. This remains unconfirmed.",
                reason: "Optional discussion of a fictional, automatically inferred eating-out routine, not a clinical finding.",
                reported: reported,
                uncertainties: ["No connected records, dates, or counts are used in this presentation-only example.", "An inferred routine does not establish consumption, diet quality, or clinical risk."],
                questions: Self.questions(for: .eatingRoutine),
                inferred: answer == .no ? [] : ["DEMO unconfirmed inference: eating out or takeaway may be a recurring routine; purchase attribution and consumption are different questions."],
                clarification: clarification
            )
        case .noFlags:
            return Self(
                status: .noConcernsFlagged, recordReview: .notReviewed,
                title: "No concerns flagged in available records",
                caption: "In this fictional example, the person reports no concern to discuss. This does not mean their health has been assessed.",
                reason: "Optional general wellbeing discussion; the fictional person reports no concern at this time.",
                reported: ["DEMO user-reported context: No concern to discuss at this time."],
                uncertainties: ["Presentation-only example, not a conclusion from normal records or hidden flags.", "No clinical assessment or complete record review has taken place."],
                questions: Self.questions(for: .noneReported)
            )
        case .insufficientData:
            return Self(
                status: .insufficientInformation, recordReview: .notReviewed,
                title: "Not enough information yet",
                caption: "This fictional example has missing or unreviewed information. It does not establish that anything is wrong or that everything is fine.",
                reason: "Confirm what you would like to discuss before preparing a personal brief.",
                reported: [],
                uncertainties: ["No connected records or confirmed eating or activity context are included."],
                questions: Self.questions(for: .notReviewed)
            )
        }
    }

    private init(status: Status, recordReview: RecordReview, title: String, caption: String,
                 reason: String, reported: [String], uncertainties: [String], questions: [String],
                 inferred: [String] = [], clarification: VisitClarification? = nil) {
        self.status = status
        self.recordReview = recordReview
        self.title = title
        self.caption = caption
        isDemo = true
        reasonForDiscussion = reason
        observedInformation = []
        inferredHabits = inferred
        self.clarification = clarification
        userReportedContext = reported
        userReportedPreferences = []
        self.uncertainties = uncertainties
        self.questions = questions
    }

    private func bullets(_ lines: [String]) -> String { lines.map { "- \($0)" }.joined(separator: "\n") }

    private static func topic(for concern: VisitConfirmedContext.Concern) -> String {
        switch concern {
        case .eatingRoutine: "eating routine"
        case .activityRoutine: "activity routine"
        default: "general wellbeing"
        }
    }

    private static func reason(for concern: VisitConfirmedContext.Concern) -> String {
        switch concern {
        case .eatingRoutine, .activityRoutine, .generalWellbeing:
            "You asked to discuss your \(topic(for: concern)); this topic comes from you, not a diagnosis from connected records."
        case .noneReported:
            "You reported no concern at this time; an optional general wellbeing discussion is still available."
        case .notReviewed:
            "No discussion topic has been confirmed; review what matters to you before using this brief."
        }
    }

    private static func questions(for concern: VisitConfirmedContext.Concern) -> [String] {
        var result = ["What context would help us discuss my wellbeing?"]
        if concern == .eatingRoutine { result.append("How could I plan an eating routine that fits my schedule and preferences?") }
        if concern == .activityRoutine { result.append("What activity options fit my circumstances and any restrictions I report?") }
        result.append("Is any follow-up useful, and what should I bring next time?")
        return result
    }

    private static func singleLine(_ input: String) -> String {
        // Keep explicit context separate from generated headings, with a bounded size.
        let clean = String(input.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) || CharacterSet.whitespacesAndNewlines.contains($0) })
        return String(clean.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ").prefix(600))
    }

    private static func date(_ input: String) -> Date? {
        guard input.utf8.count == 10,
              input.range(of: #"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"#, options: .regularExpression) != nil else { return nil }
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        guard let value = formatter.date(from: input), formatter.string(from: value) == input else { return nil }
        return value
    }

    // Mirrors contracts/scenarios.json version 1.0.0. These are operational
    // recurrence settings, not dietary guidance or clinical thresholds. A test
    // checks these values against the read-only contract to catch drift.
    static let recurrenceRuleVersion = "1.0.0"
    static let recurrenceMinimumDates = 3
    static let recurrenceMinimumSpanDays = 14
    static let recurrenceWindowDays = 28

    private struct PatternResult {
        var habits: [String] = []
        var clarification: VisitClarification?
        var reported: [String] = []
        var limitations: [String] = []
    }

    private static func latestEventFeedback(_ observation: Observation, in state: State) -> Feedback? {
        // State feedback is the committed audit order, as used by the contract's
        // eligibility overlay. Do not sort by IDs or free-form timestamps.
        state.feedback.last {
            $0.kind == "correctness" && $0.scope == "event"
                && $0.event_ref == EvidenceRef(observation_id: observation.id, revision: observation.revision)
        }
    }

    private static func isDisputed(_ feedback: Feedback?) -> Bool {
        guard let feedback else { return false }
        return feedback.value == "incorrect" || (feedback.value == "partial" && feedback.attribution == nil)
    }

    /// Invoked only after summary() verifies the card, exact active references,
    /// totals, dates, source consent, and current source availability. This adds a
    /// qualified interpretation, NEVER changes ledger suggestion eligibility.
    private static func inferPurchasePattern(
        card: LedgerCard, state: State, response: VisitClarificationResponse?
    ) -> PatternResult {
        var result = PatternResult()
        if let usefulness = state.feedback.last(where: {
            $0.card_id == card.id && $0.kind == "usefulness" && $0.scope == "card"
        }), usefulness.value != "useful" {
            result.limitations = ["You marked this pattern as not useful or only partly useful; no new personal interpretation or question is offered."]
            return result
        }
        guard state.preferences.food_suggestions, card.suggestion?.status != "suppressed",
              card.suggestion?.status != "unavailable" else {
            result.limitations = ["Food-pattern presentation is disabled or unavailable; this is not an all-clear."]
            return result
        }
        guard card.rule_version == recurrenceRuleVersion,
              let start = date(card.window.start_date), let end = date(card.window.end_date),
              card.window.end_date == state.evaluation_date,
              Int(end.timeIntervalSince(start) / 86_400) == recurrenceWindowDays - 1 else {
            result.limitations = ["This purchase window or rule version is not supported for habit inference."]
            return result
        }
        let evidence = card.evidence.compactMap { ref in
            state.observations.first { $0.id == ref.observation_id && $0.revision == ref.revision }
        }
        let usable = evidence.filter { observation in
            let feedback = latestEventFeedback(observation, in: state)
            let attribution = feedback?.attribution ?? observation.household_attribution
            return !isDisputed(feedback) && ["self", "unknown"].contains(attribution)
        }
        if usable.count != evidence.count {
            result.limitations.append("Shared, other-attributed, or disputed records are excluded from the personal pattern; observed totals may be larger.")
        }
        let dates = Set(usable.map(\.local_date)).sorted()
        guard dates.count >= recurrenceMinimumDates, let first = dates.first, let last = dates.last,
              let firstDate = date(first), let lastDate = date(last),
              Int(lastDate.timeIntervalSince(firstDate) / 86_400) >= recurrenceMinimumSpanDays else {
            result.limitations.append("The remaining purchase dates do not support the product recurrence pattern; this is not evidence of a personal habit.")
            return result
        }
        let scope = "The \(usable.count) qualifying restaurant-category purchase records on \(dates.count) dates, \(first) through \(last). This asks who the purchases were for, not whether you ate any meals."
        // Include semantic attribution/correction context as well as exact revisions.
        // Sorting makes the key stable across equivalent evidence/observation order.
        var identity = ["visit-purchase-attribution-v1", state.data_epoch, card.scenario_id,
                        card.rule_version, String(card.revision), card.mode,
                        card.window.start_date, card.window.end_date, card.window.timezone]
        for observation in evidence.sorted(by: { $0.id < $1.id }) {
            let feedback = latestEventFeedback(observation, in: state)
            identity += [observation.id, String(observation.revision), observation.source_id,
                         observation.local_date, feedback?.attribution ?? observation.household_attribution,
                         feedback?.value ?? ""]
        }
        guard let encoded = try? JSONEncoder().encode(identity) else { return result }
        let key = "visit-purchase-attribution-v1:" + SHA256.hash(data: encoded).map { String(format: "%02x", $0) }.joined()
        let answer = response?.key == key ? response?.answer : nil
        result.clarification = VisitClarification(key: key, question: "Were these purchases mostly for you?", scope: scope, answer: answer)
        result.limitations.append("The existing product recurrence rule supports a possible routine, not a clinical risk, nutritional assessment, or medical threshold.")
        switch answer {
        case .no:
            result.reported.append("Tap-confirmed context for \(first) through \(last): these purchases were not mostly for me. The personal habit inference has been removed.")
            return result
        case .yes:
            result.reported.append("Tap-confirmed context for \(first) through \(last): these purchases were mostly for me. This does not confirm consumption or diet quality.")
        case .notSure:
            result.reported.append("Tap-reported context for \(first) through \(last): not sure who these purchases were mostly for; attribution remains uncertain.")
        case .dismissed, nil:
            break
        }
        result.habits = ["Based on \(usable.count) qualifying restaurant-category purchase records on \(dates.count) dates, \(first) through \(last), eating out or takeaway may be a recurring routine. This is unconfirmed; purchases do not establish personal consumption or diet quality."]
        if answer != .yes {
            result.limitations.append("Who these purchases were mostly for is unconfirmed; the possible routine may not be personal.")
        }
        return result
    }

    private static func summary(card: LedgerCard, state: State) -> String? {
        guard card.status == "observed", !card.evidence.isEmpty,
              let start = date(card.window.start_date), let end = date(card.window.end_date), start <= end,
              let evaluation = date(state.evaluation_date), end <= evaluation,
              card.window.timezone == state.preferences.timezone,
              TimeZone(identifier: card.window.timezone) != nil,
              Set(card.evidence.map(\.observation_id)).count == card.evidence.count else { return nil }
        // Latest explicit verdict wins; old disputes do not override a later
        // correction. Current preferences encode suppression, not historical stop
        // events that the user may since have cleared. Never copy feedback prose.
        if let verdict = state.feedback.last(where: {
            $0.card_id == card.id && $0.kind == "correctness" && $0.scope == "card"
        }), verdict.value != "correct" { return nil }

        var evidence = [Observation]()
        for ref in card.evidence {
            let matching = state.observations.filter { $0.id == ref.observation_id && $0.revision == ref.revision }
            guard matching.count == 1, let observation = matching.first,
                  observation.operation == "upsert", observation.mode == card.mode,
                  observation.timezone == card.window.timezone,
                  let recorded = date(observation.local_date), recorded >= start, recorded <= end,
                  !state.observations.contains(where: {
                      ($0.id == observation.id && $0.revision > observation.revision) || $0.supersedes == ref
                  }) else { return nil }
            let sources = state.sources.filter { $0.id == observation.source_id }
            guard sources.count == 1, let source = sources.first,
                  source.enabled, source.consent == "granted", source.capability == "available",
                  source.status == "ready", state.preferences.enabled_source_ids.contains(source.id),
                  source.id != "healthkit" || source.read_access == "samples_readable" else { return nil }
            evidence.append(observation)
        }
        let prefix = card.mode == "fixture" ? "DEMO records" : "Connected records"
        let period = "\(card.window.start_date) through \(card.window.end_date)"
        if card.scenario_id == "purchase_recurrence" {
            guard Set(evidence.map(\.source_id)).count == 1, evidence.allSatisfy({
                $0.kind == "purchase" && $0.lifecycle == "posted" && $0.purchase?.is_refund == false
                    && $0.purchase?.category == "restaurant"
            }), card.purchase_count == evidence.count,
               card.distinct_purchase_dates == Set(evidence.map(\.local_date)).count else { return nil }
            let disputed = evidence.contains { isDisputed(latestEventFeedback($0, in: state)) }
            return "\(prefix), \(period): \(evidence.count) restaurant-category purchase records on \(Set(evidence.map(\.local_date)).count) dates, not a count of meals or evidence of who ate them."
                + (disputed ? " Some transaction context is disputed and excluded from habit inference." : "")
        }
        guard card.scenario_id == "recorded_steps", evidence.allSatisfy({
            $0.kind == "activity" && $0.lifecycle == "recorded" && $0.activity?.metric == "steps"
                && $0.activity?.unit == "count" && ($0.activity?.value ?? -1) >= 0
        }), Set(evidence.map(\.local_date)).count == evidence.count,
           !evidence.contains(where: { observation in
               if let feedback = latestEventFeedback(observation, in: state) { return feedback.value != "correct" }
               return false
           }) else { return nil }
        // Refuse overlapping daily records and overflow instead of inflating totals.
        var total = 0
        for observation in evidence {
            guard let value = observation.activity?.value else { return nil }
            let (sum, overflow) = total.addingReportingOverflow(value)
            guard !overflow else { return nil }
            total = sum
        }
        guard card.recorded_steps == total, card.days_with_records == evidence.count else { return nil }
        return "\(prefix), \(period): \(total) recorded steps across \(evidence.count) dates with records; coverage within those dates is unknown, and this is not total activity."
    }
}

/// Editable local text with a mandatory demo marker on copy. Creating or editing a
/// draft has no side effects; only the UI's explicit Copy action accesses a clipboard.
public struct VisitPreparationDraft: Equatable, Sendable {
    public var text: String
    public let isDemo: Bool

    public init(presentation: VisitPreparation) {
        text = presentation.brief
        isDemo = presentation.isDemo
    }

    public var textForCopy: String {
        if isDemo && !text.hasPrefix(VisitPreparation.demoLabel) {
            return VisitPreparation.demoLabel + "\n\n" + text
        }
        return text
    }
}
