import Foundation

/// A deliberately small projection, not a new health assessment or eligibility engine.
/// Pass only the visible state (nil during deletion/reset) and the actual outbox state.
public struct HomePresentation: Equatable, Sendable {
    public enum Action: Equatable, Sendable {
        case connect
        case suggestion(cardID: String, suggestionID: String, text: String)

        public var title: String {
            switch self {
            case .connect: "Connect a source"
            case .suggestion: "Explore this idea"
            }
        }

        public var detail: String? {
            if case let .suggestion(_, _, text) = self { return text }
            return nil
        }
    }

    public let observation: String
    public let caveat: String
    public let sampleDataLabel: String?
    public let action: Action?

    /// Prefers a supported card with an eligible suggestion, then recorded steps.
    /// Never exposes raw card prose, inferred claims, identifiers, dates or protocol state.
    public init(state: State?, hasPendingChanges: Bool) {
        guard let state else {
            self = Self.insufficient(sample: false, hasPendingChanges: hasPendingChanges)
            return
        }
        let candidates = state.ledger.compactMap { card -> (LedgerCard, [Observation])? in
            guard card.status == "observed",
                  ["recorded_steps", "purchase_recurrence"].contains(card.scenario_id),
                  !card.evidence.isEmpty else { return nil }
            let evidence = card.evidence.compactMap { reference in
                state.observations.first {
                    $0.id == reference.observation_id && $0.revision == reference.revision
                }
            }
            guard evidence.count == card.evidence.count,
                  Set(card.evidence.map(\.observation_id)).count == evidence.count,
                  evidence.allSatisfy({ observation in
                      observation.operation == "upsert"
                          && !state.observations.contains {
                              $0.id == observation.id && $0.revision > observation.revision
                          }
                          && state.sources.contains {
                              $0.id == observation.source_id && $0.enabled && $0.consent == "granted"
                          }
                  }) else { return nil }
            switch card.scenario_id {
            case "recorded_steps":
                guard let steps = card.recorded_steps, steps >= 0,
                      let days = card.days_with_records, days > 0,
                      evidence.allSatisfy({ $0.kind == "activity" && $0.lifecycle == "recorded" && $0.activity != nil }) else { return nil }
            case "purchase_recurrence":
                guard let count = card.purchase_count, count > 0,
                      evidence.allSatisfy({ $0.kind == "purchase" && $0.lifecycle == "posted" && $0.purchase?.is_refund == false }) else { return nil }
            default: return nil
            }
            return (card, evidence)
        }.sorted { lhs, rhs in
            let leftEligible = Self.canSuggest(lhs.0, preferences: state.preferences, pending: hasPendingChanges)
            let rightEligible = Self.canSuggest(rhs.0, preferences: state.preferences, pending: hasPendingChanges)
            if leftEligible != rightEligible { return leftEligible }
            if lhs.0.scenario_id != rhs.0.scenario_id { return lhs.0.scenario_id == "recorded_steps" }
            return lhs.0.id < rhs.0.id
        }
        guard let (card, evidence) = candidates.first else {
            self = Self.insufficient(
                sample: state.observations.contains { $0.mode == "fixture" && $0.operation == "upsert" },
                hasPendingChanges: hasPendingChanges
            )
            return
        }
        sampleDataLabel = card.mode == "fixture" || evidence.contains { $0.mode == "fixture" } ? "Sample data" : nil
        if card.scenario_id == "recorded_steps", let steps = card.recorded_steps, let days = card.days_with_records {
            observation = "Your records include \(steps.formatted()) steps across \(days) \(days == 1 ? "day" : "days")."
            caveat = "These records may cover only part of each day. Missing activity is unknown, not zero."
        } else {
            let count = card.purchase_count ?? 0
            observation = "Your records include \(count) \(count == 1 ? "purchase" : "purchases") at merchants categorized as restaurants."
            caveat = "Purchases do not tell us what you ate or who a purchase was for. Other purchases may be missing."
        }
        if Self.canSuggest(card, preferences: state.preferences, pending: hasPendingChanges), let suggestion = card.suggestion {
            action = .suggestion(cardID: card.id, suggestionID: suggestion.id, text: suggestion.text)
        } else {
            action = nil
        }
    }

    private init(observation: String, caveat: String, sampleDataLabel: String?, action: Action?) {
        self.observation = observation
        self.caveat = caveat
        self.sampleDataLabel = sampleDataLabel
        self.action = action
    }

    private static func insufficient(sample: Bool, hasPendingChanges: Bool) -> Self {
        Self(
            observation: "Not enough information yet",
            caveat: "Connect a source when you’re ready. Missing records do not tell us how you’re doing.",
            sampleDataLabel: sample ? "Sample data" : nil,
            action: hasPendingChanges ? nil : .connect
        )
    }

    private static func canSuggest(_ card: LedgerCard, preferences: Preferences, pending: Bool) -> Bool {
        guard !pending, card.status == "observed", let suggestion = card.suggestion,
              suggestion.status == "eligible", !suggestion.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !preferences.suppressed_scenario_ids.contains(card.scenario_id),
              !(suggestion.requires_kitchen && preferences.no_kitchen),
              !(suggestion.requires_spending && preferences.no_spending),
              !(suggestion.requires_movement && preferences.movement_restriction) else { return false }
        switch card.scenario_id {
        case "recorded_steps": return preferences.activity_suggestions && !preferences.movement_restriction
        case "purchase_recurrence": return preferences.food_suggestions
        default: return false
        }
    }
}
