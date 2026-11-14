import Foundation
import Testing
@testable import CompanionCore

@Suite("Isolated visit preparation")
struct VisitPreparationTests {
    private let today = "2026-11-14"

    // Uses the existing fixture read-only, with no Bundle.module/generated runner dependency.
    private func state() throws -> State {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("Fixtures/state.example.json")
        return try JSONDecoder().decode(State.self, from: Data(contentsOf: url))
    }

    private func presentation(_ state: State?, context: VisitConfirmedContext = .init(), response: VisitClarificationResponse? = nil) -> VisitPreparation {
        VisitPreparation(state: state, context: context, asOfLocalDate: today, clarificationResponse: response)
    }

    private func allCopy(_ value: VisitPreparation) -> String {
        value.title + "\n" + value.caption + "\n" + value.brief
    }

    @Test func noClinicalConcernIsInferredFromPurchasesOrPartialSteps() throws {
        let value = presentation(try state())
        #expect(value.status == .inferredPattern)
        #expect(value.title == "A possible eating-out routine")
        #expect(value.recordReview == .limited)
        #expect(value.userReportedContext.isEmpty)
        for forbidden in ["healthy", "bad eating habits", "low activity", "you ate", "you are inactive", "diabetes", "disease risk is", "specialist needed"] {
            #expect(!allCopy(value).lowercased().contains(forbidden))
        }
        #expect(value.uncertainties.contains { $0.contains("not clinical concerns") })
    }

    @Test func partialCoverageUsesOnlyActualStateCountsAndWindows() throws {
        let value = presentation(try state())
        #expect(value.observedInformation.count == 2)
        #expect(value.observedInformation[0].contains("2026-10-18 through 2026-11-14"))
        #expect(value.observedInformation[0].contains("3 restaurant-category purchase records on 3 dates"))
        #expect(value.observedInformation[1].contains("2026-11-07 through 2026-11-13"))
        #expect(value.observedInformation[1].contains("13800 recorded steps across 3 dates"))
        #expect(value.brief.contains("Missing activity records are unknown, not zero"))
        #expect(!value.brief.contains("7 fully tracked days"))
    }

    @Test func changedCountsAreNotHardCodedDemoFacts() throws {
        var snapshot = try state()
        snapshot.observations[3].activity?.value = 100
        snapshot.ledger[1].recorded_steps = 9700
        snapshot.ledger[1].window.start_date = "2026-11-08"
        let value = presentation(snapshot)
        #expect(value.observedInformation[1].contains("2026-11-08 through 2026-11-13"))
        #expect(value.observedInformation[1].contains("9700 recorded steps"))
        #expect(!value.brief.contains("13800"))
    }

    @Test func userConfirmedConcernRemainsSeparateFromObservedInformation() throws {
        let context = VisitConfirmedContext(
            confirmedConcern: .eatingRoutine,
            confirmedEatingContext: "My shifts change, and I want a more predictable eating routine."
        )
        let value = presentation(try state(), context: context)
        #expect(value.status == .userReportedConcern)
        #expect(value.reasonForDiscussion.contains("topic comes from you"))
        #expect(value.userReportedContext.contains { $0.contains("My shifts change") })
        #expect(value.userReportedPreferences.contains { $0.contains("no kitchen") })
        #expect(value.userReportedPreferences.contains { $0.contains("no-spending") })
        #expect(!value.userReportedPreferences.contains { $0.contains("movement") })
        #expect(!value.observedInformation.contains { $0.contains("My shifts change") })
        #expect(value.brief.contains("primary care or family medicine"))
        #expect(value.questions.count <= 3)
    }

    @Test func relevantActivityPreferenceIsUserReportedNotMedicalCondition() throws {
        let context = VisitConfirmedContext(confirmedConcern: .activityRoutine, confirmedActivityContext: "I prefer seated activities.")
        var snapshot = try state()
        snapshot.ledger.removeAll { $0.scenario_id == "purchase_recurrence" }
        let value = presentation(snapshot, context: context)
        #expect(value.userReportedPreferences.contains { $0.contains("avoid movement suggestions; this does not specify a medical condition") })
        #expect(!value.userReportedPreferences.contains { $0.contains("kitchen") })
        #expect(value.userReportedContext.contains { $0.contains("I prefer seated activities") })
    }

    @Test func sharedOtherOrUnknownRecordsNeverBecomePersonalBehaviorClaims() throws {
        for attribution in ["shared", "other", "unknown"] {
            var snapshot = try state()
            for index in snapshot.observations.indices { snapshot.observations[index].household_attribution = attribution }
            let value = presentation(snapshot)
            #expect(value.status == (attribution == "unknown" ? .inferredPattern : .insufficientInformation))
            #expect(value.observedInformation.count == 2)
            #expect(!value.observedInformation.joined().lowercased().contains("your"))
            #expect(!value.observedInformation.joined().contains("You"))
            #expect(value.uncertainties.contains { $0.contains("not confirmed personal eating or activity") })
        }
    }

    @Test func noReportedConcernDoesNotClearPartialOrUnreviewedRecords() throws {
        for snapshot in [try state(), nil] {
            let value = presentation(snapshot, context: .init(confirmedConcern: .noneReported))
            #expect(value.status == .insufficientInformation)
            #expect(value.title != "No concerns flagged in available records")
            #expect(value.brief.contains("not a health assessment"))
        }
    }

    @Test func missingDisconnectedAndUnreadyDataNeverBecomeClear() throws {
        let empty = presentation(nil)
        #expect(empty.recordReview == .notReviewed)
        #expect(empty.observedInformation.isEmpty)
        for status in ["disabled", "syncing", "empty", "stale", "error", "not_configured"] {
            var snapshot = try state()
            snapshot.sources[0].status = status
            let value = presentation(snapshot)
            #expect(value.status == .insufficientInformation)
            #expect(value.recordReview == .notReviewed)
            #expect(value.observedInformation.isEmpty)
        }
        var snapshot = try state()
        snapshot.sources = []
        #expect(presentation(snapshot).observedInformation.isEmpty)
        snapshot = try state()
        snapshot.sources[0].consent = "revoked"
        #expect(presentation(snapshot).observedInformation.isEmpty)
        snapshot = try state()
        snapshot.preferences.enabled_source_ids = []
        #expect(presentation(snapshot).observedInformation.isEmpty)
    }

    @Test func pendingConflictedOrStaleSnapshotIsNotReviewed() throws {
        let snapshot = try state()
        let pending = VisitPreparation(state: snapshot, asOfLocalDate: today, hasPendingChanges: true)
        let conflict = VisitPreparation(state: snapshot, asOfLocalDate: today, hasConflict: true)
        let old = VisitPreparation(state: snapshot, asOfLocalDate: "2026-11-15")
        let invalidDate = VisitPreparation(state: snapshot, asOfLocalDate: "not-a-date")
        for value in [pending, conflict, old, invalidDate] {
            #expect(value.recordReview == .notReviewed)
            #expect(value.status == .insufficientInformation)
            #expect(value.observedInformation.isEmpty)
        }
    }

    @Test func missingOrRunningLedgerDoesNotClear() throws {
        var snapshot = try state()
        snapshot.ledger = []
        #expect(presentation(snapshot).recordReview == .notReviewed)
        snapshot = try state()
        snapshot.process.status = "running"
        #expect(presentation(snapshot).recordReview == .notReviewed)
        snapshot.process.status = "completed"
        snapshot.process.ledger_status = "failed"
        #expect(presentation(snapshot).recordReview == .notReviewed)
    }

    @Test func userReportedConcernDoesNotRequireOrInventRecords() {
        let value = presentation(nil, context: .init(confirmedConcern: .eatingRoutine, confirmedEatingContext: "I want to discuss meal planning."))
        #expect(value.status == .userReportedConcern)
        #expect(value.recordReview == .notReviewed)
        #expect(value.observedInformation.isEmpty)
        #expect(value.brief.contains("No current, verified record summary"))
        #expect(value.brief.contains("User-reported eating context"))
    }

    @Test func suppressionCannotBecomeAnAllClear() throws {
        var snapshot = try state()
        snapshot.preferences.suppressed_scenario_ids = ["purchase_recurrence", "recorded_steps"]
        let value = presentation(snapshot, context: .init(confirmedConcern: .noneReported))
        #expect(value.status == .insufficientInformation)
        #expect(value.observedInformation.isEmpty)
        #expect(value.uncertainties.contains { $0.contains("hidden items do not mean there are no concerns") })
        snapshot.preferences.suppressed_scenario_ids = []
        snapshot.ledger[0].status = "suppressed"
        #expect(presentation(snapshot).observedInformation.count == 1)
        // A movement suggestion is suppressed in the original fixture; its valid
        // observations are still available, with an explicit hidden-information caveat.
        #expect(presentation(try state()).observedInformation.count == 2)
        #expect(presentation(try state()).uncertainties.contains { $0.contains("hidden") })
    }

    @Test func oldReferencesSupersededRemovedAndPendingRecordsAreExcluded() throws {
        var snapshot = try state()
        snapshot.ledger[0].evidence[0].revision = 99
        #expect(presentation(snapshot).observedInformation.count == 1)
        snapshot = try state()
        var revised = snapshot.observations[0]
        revised.revision = 2
        snapshot.observations.append(revised)
        #expect(presentation(snapshot).observedInformation.count == 1)
        snapshot = try state()
        var successor = snapshot.observations[0]
        successor.id = "new-posted-id"
        successor.supersedes = snapshot.ledger[0].evidence[0]
        snapshot.observations.append(successor)
        #expect(presentation(snapshot).observedInformation.count == 1)
        for lifecycle in ["pending", "removed"] {
            snapshot = try state()
            snapshot.observations[0].lifecycle = lifecycle
            #expect(presentation(snapshot).observedInformation.count == 1)
        }
        snapshot = try state()
        snapshot.observations[0].operation = "remove"
        #expect(presentation(snapshot).observedInformation.count == 1)
        snapshot = try state()
        snapshot.observations[0].purchase?.is_refund = true
        #expect(presentation(snapshot).observedInformation.count == 1)
    }

    @Test func mismatchedTotalsDuplicateRefsAndOverlappingStepsAreNotSummarized() throws {
        var snapshot = try state()
        snapshot.ledger[0].purchase_count = 100
        snapshot.ledger[1].recorded_steps = 99999
        #expect(presentation(snapshot).observedInformation.isEmpty)
        snapshot = try state()
        snapshot.ledger[0].evidence.append(snapshot.ledger[0].evidence[0])
        #expect(presentation(snapshot).observedInformation.count == 1)
        snapshot = try state()
        snapshot.observations.append(snapshot.observations[0])
        #expect(presentation(snapshot).observedInformation.count == 1)
        snapshot = try state()
        snapshot.observations[4].local_date = snapshot.observations[3].local_date
        #expect(presentation(snapshot).observedInformation.count == 1)
        snapshot = try state()
        snapshot.observations[3].activity?.value = Int.max
        #expect(presentation(snapshot).observedInformation.count == 1)
    }

    @Test func disputedFeedbackBlocksRelevantSummaryWithoutLeakingNotes() throws {
        var snapshot = try state()
        snapshot.feedback = [Feedback(id: "private-feedback-id", card_id: snapshot.ledger[0].id,
            kind: "correctness", value: "incorrect", scope: "card", event_ref: nil,
            attribution: nil, note: "PRIVATE_FEEDBACK", created_at: "2026-11-14T12:00:00Z")]
        let value = presentation(snapshot)
        #expect(value.observedInformation.count == 1)
        #expect(!value.brief.contains("PRIVATE_FEEDBACK"))
        #expect(!value.brief.contains("private-feedback-id"))
    }

    @Test func unrelatedPrivateNotesProviderFieldsAndRawProseAreExcluded() throws {
        var snapshot = try state()
        let sentinel = "SECRET_PRIVATE_SENTINEL"
        snapshot.preferences.notes = sentinel
        snapshot.data_epoch = sentinel
        snapshot.sources[0].reason = sentinel
        snapshot.process.run_id = sentinel
        snapshot.limitations = [sentinel]
        for index in snapshot.ledger.indices {
            snapshot.ledger[index].observed = sentinel
            snapshot.ledger[index].inferred = sentinel + " healthy low activity bad eating habits"
            snapshot.ledger[index].title = sentinel
            snapshot.ledger[index].limitations = [sentinel]
            snapshot.ledger[index].suggestion?.text = sentinel
            snapshot.ledger[index].suggestion?.reason = sentinel
        }
        for index in snapshot.observations.indices {
            snapshot.observations[index].purchase?.merchant = sentinel
            snapshot.observations[index].provenance.record_id = sentinel
            snapshot.observations[index].provenance.adapter_version = sentinel
        }
        let value = presentation(snapshot, context: .init(confirmedConcern: .eatingRoutine))
        #expect(value.observedInformation.count == 2)
        #expect(!allCopy(value).contains(sentinel))
        for forbidden in ["demo-purchase-1", "demo-steps-", "purchase_recurrence", "recorded_steps", "demo_fixture", "Synthetic Cafe"] {
            #expect(!allCopy(value).contains(forbidden))
        }
    }

    @Test func invalidWindowOrInjectedDateIsNeverRendered() throws {
        for date in ["2026-11-31", "2026-11-14\nSECRET", "2027-01-01"] {
            var snapshot = try state()
            snapshot.ledger[0].window.end_date = date
            let value = presentation(snapshot)
            #expect(value.observedInformation.count == 1)
            #expect(!value.brief.contains("SECRET"))
        }
        var snapshot = try state()
        snapshot.observations[0].local_date = "2025-01-01"
        #expect(presentation(snapshot).observedInformation.count == 1)
    }

    @Test func captionStaysShortEvenWithLongMultisentenceUserContext() throws {
        let context = VisitConfirmedContext(confirmedConcern: .eatingRoutine,
            confirmedEatingContext: String(repeating: "One. Two. Three.\n", count: 200))
        let value = presentation(try state(), context: context)
        for item in [value] + VisitPreparation.Demo.allCases.map(VisitPreparation.demo) {
            #expect(item.caption.filter { ".!?".contains($0) }.count <= 2)
            #expect(item.caption.count < 240)
        }
        let reported = try #require(value.userReportedContext.first { $0.hasPrefix("User-reported eating context:") })
        #expect(!reported.contains("\n"))
        #expect(reported.count < 650)
        #expect(!value.caption.contains("One"))
    }

    @Test func demosAreExplicitAndDoNotMutateOrPretendToHaveStateCounts() throws {
        let snapshot = try state()
        let before = snapshot
        for demo in VisitPreparation.Demo.allCases {
            let value = VisitPreparation.demo(demo)
            #expect(value.isDemo)
            #expect(value.brief.hasPrefix(VisitPreparation.demoLabel))
            #expect(value.observedInformation.isEmpty)
            #expect(value.recordReview == .notReviewed)
            #expect(!value.brief.contains("13800"))
            #expect(!value.brief.contains("2026-"))
        }
        #expect(VisitPreparation.demo(.noFlags).title == "No concerns flagged in available records")
        #expect(VisitPreparation.demo(.noFlags).status == .noConcernsFlagged)
        #expect(VisitPreparation.demo(.eatingRoutine).status == .inferredPattern)
        #expect(VisitPreparation.demo(.insufficientData).title == "Not enough information yet")
        #expect(snapshot == before)
    }

    @Test func fixtureBackedRealProjectionAlsoCarriesDemoLabel() throws {
        let value = presentation(try state())
        #expect(value.isDemo)
        #expect(value.brief.hasPrefix(VisitPreparation.demoLabel))
        #expect(value.observedInformation.allSatisfy { $0.hasPrefix("DEMO records") })
    }

    @Test func editableDraftCopiesEditsAndCannotLoseDemoMarker() throws {
        let value = VisitPreparation.demo(.eatingRoutine)
        var draft = VisitPreparationDraft(presentation: value)
        #expect(draft.text == value.brief)
        draft.text = "My edited discussion points."
        #expect(draft.textForCopy == VisitPreparation.demoLabel + "\n\nMy edited discussion points.")
        #expect(value.brief != draft.text)
        var live = VisitPreparationDraft(presentation: presentation(nil))
        live.text = "Only this text should be copied."
        #expect(live.textForCopy == live.text)
        #expect(!live.isDemo)
    }

    @Test func operationalRecurrenceSettingsMatchFrozenContract() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../contracts/scenarios.json").standardizedFileURL
        let object = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let rule = try #require(object["purchase_recurrence"] as? [String: Any])
        #expect(object["rule_version"] as? String == VisitPreparation.recurrenceRuleVersion)
        #expect(rule["minimum_distinct_dates"] as? Int == VisitPreparation.recurrenceMinimumDates)
        #expect(rule["minimum_span_days"] as? Int == VisitPreparation.recurrenceMinimumSpanDays)
        #expect(rule["window_days_including_today"] as? Int == VisitPreparation.recurrenceWindowDays)
    }

    @Test func meaningfulRecurrenceProducesAutomaticInferenceWithNoTypedOrConfirmedContext() throws {
        var snapshot = try state()
        for index in 0..<3 { snapshot.observations[index].household_attribution = "unknown" }
        // The frozen ledger cannot offer a personal suggestion for unknown
        // attribution. The new qualified presentation must not mutate that rule.
        snapshot.ledger[0].suggestion = nil
        let before = snapshot
        let value = presentation(snapshot)
        #expect(value.status == .inferredPattern)
        #expect(value.caption.contains("suggest eating out may be frequent"))
        #expect(value.inferredHabits.count == 1)
        #expect(value.inferredHabits[0].contains("unconfirmed"))
        #expect(value.userReportedContext.isEmpty)
        #expect(value.brief.contains("Inferred habit — unconfirmed"))
        #expect(value.brief.contains("User-reported context — optional"))
        #expect(value.brief.contains("no answer or typing is required"))
        #expect(value.userReportedPreferences.contains { $0.contains("no kitchen") })
        #expect(value.userReportedPreferences.contains { $0.contains("no-spending") })
        #expect(!value.userReportedPreferences.contains { $0.contains("movement") })
        #expect(!VisitPreparationDraft(presentation: value).textForCopy.isEmpty)
        let question = try #require(value.clarification)
        #expect(question.question == "Were these purchases mostly for you?")
        #expect(question.scope.contains("3 qualifying restaurant-category purchase records"))
        #expect(question.scope.contains("2026-10-22 through 2026-11-10"))
        #expect(question.scope.contains("not whether you ate any meals"))
        #expect(question.shouldPrompt)
        #expect(question.answer == nil)
        #expect(!value.brief.contains(question.key))
        #expect(snapshot == before)
        #expect(snapshot.ledger[0].suggestion == nil)
    }

    @Test func yesConfirmsOnlyAttributionAndDoesNotVerifyHabitOrConsumption() throws {
        let snapshot = try state()
        let initial = presentation(snapshot)
        let key = try #require(initial.clarification?.key)
        let value = presentation(snapshot, response: .init(key: key, answer: .yes))
        #expect(value.status == .inferredPattern)
        #expect(value.inferredHabits == initial.inferredHabits)
        #expect(value.userReportedContext.contains { $0.contains("Tap-confirmed") && $0.contains("mostly for me") })
        #expect(value.userReportedContext.contains { $0.contains("does not confirm consumption") })
        #expect(value.clarification?.shouldPrompt == false)
        #expect(value.clarification?.answer == .yes)
        #expect(!value.reasonForDiscussion.contains("You asked"))
    }

    @Test func noOverridesPersonalInferenceWithoutErasingObservedFactsOrIndependentConcern() throws {
        let snapshot = try state()
        let initial = presentation(snapshot)
        let response = VisitClarificationResponse(key: try #require(initial.clarification?.key), answer: .no)
        let value = presentation(snapshot, response: response)
        #expect(value.inferredHabits.isEmpty)
        #expect(value.status == .insufficientInformation)
        #expect(value.observedInformation == initial.observedInformation)
        #expect(value.userReportedContext.contains { $0.contains("not mostly for me") })
        #expect(value.caption.contains("personal inference was removed"))
        #expect(value.clarification?.shouldPrompt == false)
        #expect(!value.brief.contains("eating out may be frequent"))
        let independent = presentation(snapshot, context: .init(confirmedConcern: .eatingRoutine), response: response)
        #expect(independent.status == .userReportedConcern)
        #expect(independent.inferredHabits.isEmpty)
        #expect(independent.reasonForDiscussion.contains("topic comes from you"))
    }

    @Test func unsureAndDismissalRetainUncertaintyWithoutReprompting() throws {
        let snapshot = try state()
        let initial = presentation(snapshot)
        let key = try #require(initial.clarification?.key)
        for answer in [VisitClarificationResponse.Answer.notSure, .dismissed] {
            let response = VisitClarificationResponse(key: key, answer: answer)
            let roundTrip = try JSONDecoder().decode(VisitClarificationResponse.self, from: JSONEncoder().encode(response))
            for _ in 0..<3 {
                let value = presentation(snapshot, response: roundTrip)
                #expect(value.status == .inferredPattern)
                #expect(value.clarification?.shouldPrompt == false)
                #expect(value.inferredHabits == initial.inferredHabits)
                #expect(value.uncertainties.contains { $0.contains("possible routine may not be personal") })
                if answer == .notSure {
                    #expect(value.userReportedContext.contains { $0.contains("not sure") })
                    #expect(!value.userReportedContext.contains { $0.contains("Tap-confirmed") })
                } else {
                    #expect(value.userReportedContext.isEmpty)
                }
            }
        }
    }

    @Test func clarificationKeyIsStableAcrossLaunchEquivalentOrderingAndUnrelatedChanges() throws {
        var snapshot = try state()
        let initial = presentation(snapshot)
        let key = try #require(initial.clarification?.key)
        snapshot.observations.reverse()
        snapshot.ledger[0].evidence.reverse()
        snapshot.revision += 1
        snapshot.generated_at = "2026-11-14T15:00:00Z"
        snapshot.preferences.notes = "PRIVATE_UNRELATED_NOTE"
        snapshot.ledger[1].revision += 1
        #expect(presentation(snapshot).clarification?.key == key)
        let response = VisitClarificationResponse(key: key, answer: .notSure)
        #expect(presentation(snapshot, response: response).clarification?.shouldPrompt == false)
        #expect(!key.contains(snapshot.data_epoch))
        #expect(!key.contains("demo-purchase-1"))
    }

    @Test func epochPatternAndEvidenceRevisionChangesRejectOldAnswers() throws {
        let baseline = try state()
        let key = try #require(presentation(baseline).clarification?.key)
        let old = VisitClarificationResponse(key: key, answer: .no)
        var epoch = baseline
        epoch.data_epoch = "a-new-account-or-reset-epoch"
        var card = baseline
        card.ledger[0].revision += 1
        var evidence = baseline
        evidence.observations[0].revision += 1
        evidence.observations[0].supersedes = baseline.ledger[0].evidence[0]
        evidence.ledger[0].evidence[0].revision += 1
        for snapshot in [epoch, card, evidence] {
            let value = presentation(snapshot, response: old)
            #expect(value.clarification?.key != key)
            #expect(value.clarification?.answer == nil)
            #expect(value.clarification?.shouldPrompt == true)
            #expect(value.status == .inferredPattern)
            #expect(!value.userReportedContext.contains { $0.contains("not mostly for me") })
        }
        #expect(presentation(nil, response: old).clarification == nil)
    }

    private func eventCorrection(_ snapshot: State, index: Int = 0, value: String, attribution: String? = nil) -> Feedback {
        Feedback(id: "correction-\(index)-\(value)", card_id: snapshot.ledger[0].id,
                 kind: "correctness", value: value, scope: "event", event_ref: snapshot.ledger[0].evidence[index],
                 attribution: attribution, note: "PRIVATE_CORRECTION_NOTE", created_at: "2026-11-14T12:00:00Z")
    }

    @Test func explicitAttributionCorrectionOverridesSourceAndOldYesAnswer() throws {
        for attribution in ["shared", "other"] {
            var snapshot = try state()
            let key = try #require(presentation(snapshot).clarification?.key)
            snapshot.feedback.append(eventCorrection(snapshot, value: "partial", attribution: attribution))
            let before = snapshot
            let value = presentation(snapshot, response: .init(key: key, answer: .yes))
            #expect(value.status == .insufficientInformation)
            #expect(value.inferredHabits.isEmpty)
            #expect(value.observedInformation.count == 2)
            #expect(value.clarification == nil)
            #expect(!value.userReportedContext.contains { $0.contains("mostly for me") })
            #expect(!value.brief.contains("PRIVATE_CORRECTION_NOTE"))
            #expect(snapshot == before)
        }
    }

    @Test func latestExplicitFeedbackRestoresCorrectedEvidenceNotHistoricalDispute() throws {
        var snapshot = try state()
        snapshot.feedback.append(eventCorrection(snapshot, value: "incorrect"))
        #expect(presentation(snapshot).inferredHabits.isEmpty)
        #expect(presentation(snapshot).observedInformation[0].contains("disputed"))
        snapshot.feedback.append(eventCorrection(snapshot, value: "correct"))
        #expect(presentation(snapshot).status == .inferredPattern)
        snapshot.feedback.append(Feedback(id: "card-dispute", card_id: snapshot.ledger[0].id,
            kind: "correctness", value: "partial", scope: "card", event_ref: nil, attribution: nil,
            note: "PRIVATE", created_at: "2026-11-14T12:00:00Z"))
        #expect(presentation(snapshot).inferredHabits.isEmpty)
        var restored = snapshot.feedback.last!
        restored.id = "card-restored"
        restored.value = "correct"
        snapshot.feedback.append(restored)
        #expect(presentation(snapshot).status == .inferredPattern)
    }

    @Test func partialDisputeWithoutAttributionAbstainsButUnknownCorrectionStaysQualified() throws {
        var snapshot = try state()
        snapshot.feedback.append(eventCorrection(snapshot, value: "partial"))
        #expect(presentation(snapshot).inferredHabits.isEmpty)
        snapshot.feedback.append(eventCorrection(snapshot, value: "partial", attribution: "unknown"))
        let value = presentation(snapshot)
        #expect(value.status == .inferredPattern)
        #expect(value.userReportedContext.isEmpty)
        #expect(value.uncertainties.contains { $0.contains("may not be personal") })
    }

    @Test func mixedAttributionUsesOnlyQualifyingSubsetAndNeverInflatesItsCounts() throws {
        var snapshot = try state()
        for index in 0..<3 { snapshot.observations[index].household_attribution = "unknown" }
        var extra = snapshot.observations[0]
        extra.id = "shared-extra"
        extra.local_date = "2026-11-12"
        extra.purchase?.authorized_date = extra.local_date
        extra.purchase?.posted_date = extra.local_date
        extra.household_attribution = "shared"
        snapshot.observations.append(extra)
        snapshot.ledger[0].evidence.append(.init(observation_id: extra.id, revision: extra.revision))
        snapshot.ledger[0].purchase_count = 4
        snapshot.ledger[0].distinct_purchase_dates = 4
        snapshot.ledger[0].suggestion = nil
        let value = presentation(snapshot)
        #expect(value.observedInformation[0].contains("4 restaurant-category purchase records"))
        #expect(value.inferredHabits[0].contains("3 qualifying restaurant-category purchase records"))
        #expect(value.clarification?.scope.contains("3 qualifying") == true)
        #expect(value.clarification?.scope.contains("2026-11-12") == false)
        #expect(value.uncertainties.contains { $0.contains("observed totals may be larger") })
        snapshot.observations[0].household_attribution = "other"
        #expect(presentation(snapshot).inferredHabits.isEmpty)
        #expect(presentation(snapshot).status == .insufficientInformation)
    }

    @Test func sparseOrShortSpanPurchasesCannotTriggerHabitInferenceEvenWithRawInferredProse() throws {
        var sparse = try state()
        sparse.ledger[0].evidence.removeLast()
        sparse.ledger[0].purchase_count = 2
        sparse.ledger[0].distinct_purchase_dates = 2
        sparse.ledger[0].inferred = "An alleged daily eating habit"
        #expect(presentation(sparse).status == .insufficientInformation)
        #expect(presentation(sparse).clarification == nil)
        var short = try state()
        for index in 0..<3 {
            let localDate = "2026-11-1\(index)"
            short.observations[index].local_date = localDate
            short.observations[index].purchase?.authorized_date = localDate
            short.observations[index].purchase?.posted_date = localDate
        }
        #expect(presentation(short).observedInformation.count == 2)
        #expect(presentation(short).status == .insufficientInformation)
        #expect(presentation(short).inferredHabits.isEmpty)
    }

    @Test func suppressedDisabledOrUnavailablePatternsCannotCreateConcernOrQuestion() throws {
        let baseline = try state()
        var disabled = baseline
        disabled.preferences.food_suggestions = false
        var suppressed = baseline
        suppressed.ledger[0].suggestion?.status = "suppressed"
        var unavailable = baseline
        unavailable.ledger[0].suggestion?.status = "unavailable"
        var scenario = baseline
        scenario.preferences.suppressed_scenario_ids = ["purchase_recurrence"]
        for snapshot in [disabled, suppressed, unavailable, scenario] {
            let value = presentation(snapshot)
            #expect(value.status == .insufficientInformation)
            #expect(value.inferredHabits.isEmpty)
            #expect(value.clarification == nil)
            #expect(value.status != .noConcernsFlagged)
        }
    }

    @Test func explicitNotUsefulPreventsRepeatedQuestionEvenWithoutLedgerSuggestion() throws {
        var snapshot = try state()
        snapshot.ledger[0].suggestion = nil
        snapshot.feedback.append(Feedback(id: "usefulness-verdict", card_id: snapshot.ledger[0].id,
            kind: "usefulness", value: "not_useful", scope: "card", event_ref: nil, attribution: nil,
            note: "PRIVATE", created_at: "2026-11-14T12:00:00Z"))
        let value = presentation(snapshot)
        #expect(value.status == .insufficientInformation)
        #expect(value.observedInformation.count == 2)
        #expect(value.inferredHabits.isEmpty)
        #expect(value.clarification == nil)
        var restored = snapshot.feedback.last!
        restored.id = "useful-again"
        restored.value = "useful"
        snapshot.feedback.append(restored)
        #expect(presentation(snapshot).status == .inferredPattern)
    }

    @Test func mixedModesOrSourcesAndUnsupportedRulesDoNotCreateHabitInference() throws {
        let baseline = try state()
        var mode = baseline
        mode.observations[0].mode = "live"
        var source = baseline
        source.sources[2] = source.sources[0]
        source.sources[2].id = "plaid"
        source.preferences.enabled_source_ids.append("plaid")
        source.observations[0].source_id = "plaid"
        var rule = baseline
        rule.ledger[0].rule_version = "future-unrecognized-rule"
        for snapshot in [mode, source, rule] {
            let value = presentation(snapshot)
            #expect(value.inferredHabits.isEmpty)
            #expect(value.status == .insufficientInformation)
            #expect(value.clarification == nil)
        }
    }

    @Test func captionsAndProvenanceStaySafeForEveryTapOutcomeWithoutTypedInput() throws {
        let snapshot = try state()
        let key = try #require(presentation(snapshot).clarification?.key)
        for answer in VisitClarificationResponse.Answer.allCases {
            let value = presentation(snapshot, response: .init(key: key, answer: answer))
            #expect(value.caption.filter { ".!?".contains($0) }.count <= 2)
            #expect(value.caption.count < 240)
            #expect(!value.brief.contains("User-reported eating context:"))
            for phrase in ["healthy", "bad eating habits", "you ate", "diabetes", "heart disease", "low activity", "calories", "need a specialist", "you must see"] {
                #expect(!allCopy(value).lowercased().contains(phrase))
            }
            #expect(value.brief.contains("Observed information"))
            #expect(value.brief.contains("Inferred habit — unconfirmed"))
            #expect(value.brief.contains("User-reported context — optional"))
        }
    }

    @Test func demoCanBeUsedAndCorrectedEntirelyWithTaps() throws {
        let initial = VisitPreparation.demo(.eatingRoutine)
        #expect(initial.status == .inferredPattern)
        #expect(initial.userReportedContext.isEmpty)
        let key = try #require(initial.clarification?.key)
        for answer in VisitClarificationResponse.Answer.allCases {
            let value = VisitPreparation.demo(.eatingRoutine, clarificationResponse: .init(key: key, answer: answer))
            #expect(value.clarification?.shouldPrompt == false)
            #expect(value.isDemo)
            #expect(VisitPreparationDraft(presentation: value).textForCopy.hasPrefix(VisitPreparation.demoLabel))
            #expect(value.inferredHabits.isEmpty == (answer == .no))
        }
    }

    @Test func deterministicAndReadOnly() throws {
        let snapshot = try state()
        let before = snapshot
        let context = VisitConfirmedContext(confirmedConcern: .eatingRoutine, confirmedEatingContext: "Changing schedule")
        #expect(presentation(snapshot, context: context) == presentation(snapshot, context: context))
        #expect(snapshot == before)
        #expect(VisitPreparation.doctorStartingPoint.contains("nonurgent"))
        #expect(presentation(snapshot).brief.contains("Nothing has been sent to a doctor"))
    }
}
