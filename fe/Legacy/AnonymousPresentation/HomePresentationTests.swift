import Foundation
import Testing
@testable import CompanionCore

@Suite("Minimal Home presentation")
struct HomePresentationTests {
    private func sample() throws -> State {
        let url = try #require(Bundle.module.url(forResource: "state.example", withExtension: "json", subdirectory: "Fixtures"))
        return try JSONDecoder().decode(State.self, from: Data(contentsOf: url))
    }

    @Test func eligibleSupportedCardProvidesOneAction() throws {
        let state = try sample()
        let home = HomePresentation(state: state, hasPendingChanges: false)
        let card = try #require(state.ledger.first { $0.scenario_id == "purchase_recurrence" })
        let suggestion = try #require(card.suggestion)
        #expect(home.action == .suggestion(cardID: card.id, suggestionID: suggestion.id, text: suggestion.text))
        #expect(home.action?.title == "Explore this idea")
    }

    @Test(arguments: ["suppressed", "unavailable"])
    func ineligibleSuggestionIsAbsent(status: String) throws {
        var state = try sample()
        for i in state.ledger.indices { state.ledger[i].suggestion?.status = status }
        #expect(HomePresentation(state: state, hasPendingChanges: false).action == nil)
    }

    @Test func pendingChangesSuppressAllActions() throws {
        let state = try sample()
        #expect(HomePresentation(state: state, hasPendingChanges: true).action == nil)
        #expect(HomePresentation(state: nil, hasPendingChanges: true).action == nil)
    }

    @Test func missingDataIsUnknownWithConnectAction() throws {
        var state = try sample()
        state.observations = []
        for home in [HomePresentation(state: nil, hasPendingChanges: false), HomePresentation(state: state, hasPendingChanges: false)] {
            #expect(home.observation == "Not enough information yet")
            #expect(home.action == .connect)
            #expect(home.sampleDataLabel == nil)
            #expect(!home.observation.contains("healthy"))
        }
    }

    @Test func insufficientAndPreferenceCardsAreNotWellbeingEvidence() throws {
        var state = try sample()
        for i in state.ledger.indices where state.ledger[i].scenario_id != "preference_filter" {
            state.ledger[i].status = "insufficient_evidence"
        }
        let home = HomePresentation(state: state, hasPendingChanges: false)
        #expect(home.observation == "Not enough information yet")
        #expect(home.action == .connect)
    }

    @Test func fixtureLabelIsShortAndVisibleData() throws {
        let home = HomePresentation(state: try sample(), hasPendingChanges: false)
        #expect(home.sampleDataLabel == "Sample data")
        #expect(!home.observation.contains("fixture"))
    }

    @Test func evidenceFixtureCannotBeDisguisedByCardMode() throws {
        var state = try sample()
        for i in state.ledger.indices { state.ledger[i].mode = "live" }
        #expect(HomePresentation(state: state, hasPendingChanges: false).sampleDataLabel == "Sample data")
    }

    @Test func liveSelectionIsNotLabeledByUnrelatedFixtures() throws {
        var state = try sample()
        state.preferences.food_suggestions = false
        let index = try #require(state.ledger.firstIndex { $0.scenario_id == "recorded_steps" })
        state.ledger[index].mode = "live"
        let ids = Set(state.ledger[index].evidence.map(\.observation_id))
        for i in state.observations.indices where ids.contains(state.observations[i].id) {
            state.observations[i].mode = "live"
        }
        #expect(HomePresentation(state: state, hasPendingChanges: false).sampleDataLabel == nil)
    }

    @Test func purchaseWordingDoesNotInferMealsOrHealth() throws {
        var state = try sample()
        let i = try #require(state.ledger.firstIndex { $0.scenario_id == "purchase_recurrence" })
        state.ledger[i].observed = "Internal raw title, dates and revisions"
        state.ledger[i].inferred = "You ate three meals and too many calories"
        let home = HomePresentation(state: state, hasPendingChanges: false)
        #expect(home.observation == "Your records include 3 purchases at merchants categorized as restaurants.")
        #expect(home.caveat.contains("do not tell us what you ate"))
        for forbidden in ["meals", "calories", "healthy", "revision", "2026-", "purchase_recurrence", "Internal"] {
            #expect(!home.observation.contains(forbidden))
        }
    }

    @Test func suppressionPreferenceWinsOverStaleEligibility() throws {
        var state = try sample()
        state.preferences.suppressed_scenario_ids = ["purchase_recurrence", "recorded_steps"]
        for i in state.ledger.indices { state.ledger[i].suggestion?.status = "eligible" }
        #expect(HomePresentation(state: state, hasPendingChanges: false).action == nil)
    }

    @Test func constraintsWinOverStaleEligibility() throws {
        var state = try sample()
        let index = try #require(state.ledger.firstIndex { $0.scenario_id == "purchase_recurrence" })
        state.ledger[index].suggestion?.requires_spending = true
        #expect(HomePresentation(state: state, hasPendingChanges: false).action == nil)
        state.ledger[index].suggestion?.requires_spending = false
        state.ledger[index].suggestion?.requires_kitchen = true
        #expect(HomePresentation(state: state, hasPendingChanges: false).action == nil)
        state.preferences.food_suggestions = false
        state.ledger[index].suggestion?.requires_kitchen = false
        for i in state.ledger.indices { state.ledger[i].suggestion?.status = "eligible" }
        #expect(HomePresentation(state: state, hasPendingChanges: false).action == nil)
    }

    @Test func revokedSourceDoesNotSupplyAnObservation() throws {
        var state = try sample()
        for i in state.sources.indices { state.sources[i].consent = "revoked" }
        #expect(HomePresentation(state: state, hasPendingChanges: false).observation == "Not enough information yet")
    }

    @Test func missingOrSupersededEvidenceDoesNotSupplyAnObservation() throws {
        var state = try sample()
        state.ledger = state.ledger.filter { $0.scenario_id == "purchase_recurrence" }
        let ref = try #require(state.ledger.first?.evidence.first)
        let old = try #require(state.observations.first { $0.id == ref.observation_id })
        var removed = old
        removed.revision += 1
        removed.operation = "remove"
        removed.lifecycle = "removed"
        state.observations.append(removed)
        #expect(HomePresentation(state: state, hasPendingChanges: false).observation == "Not enough information yet")
        state.observations.removeAll { $0.id == ref.observation_id }
        #expect(HomePresentation(state: state, hasPendingChanges: false).observation == "Not enough information yet")
    }

    @Test func explicitZeroRemainsAnObservationNotAnAssessment() throws {
        var state = try sample()
        state.ledger = state.ledger.filter { $0.scenario_id == "recorded_steps" }
        state.ledger[0].recorded_steps = 0
        let home = HomePresentation(state: state, hasPendingChanges: false)
        #expect(home.observation == "Your records include 0 steps across 3 days.")
        #expect(home.caveat.contains("Missing activity is unknown, not zero"))
        #expect(home.action == nil)
    }

    @Test func deterministicChoiceDoesNotDependOnLedgerOrder() throws {
        var state = try sample()
        let original = HomePresentation(state: state, hasPendingChanges: false)
        state.ledger.reverse()
        #expect(HomePresentation(state: state, hasPendingChanges: false) == original)
    }

    @Test func invalidCountsAndBlankSuggestionsAreNotShown() throws {
        var state = try sample()
        for i in state.ledger.indices { state.ledger[i].suggestion?.text = " \n " }
        #expect(HomePresentation(state: state, hasPendingChanges: false).action == nil)
        for i in state.ledger.indices {
            state.ledger[i].purchase_count = -1
            state.ledger[i].recorded_steps = -1
        }
        #expect(HomePresentation(state: state, hasPendingChanges: false).observation == "Not enough information yet")
    }
}
