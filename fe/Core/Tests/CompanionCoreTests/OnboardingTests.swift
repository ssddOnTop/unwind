import Foundation
import Testing
@testable import CompanionCore

/// Local-only synthetic transport: no providers, OS prompts, accounts, or runtime fixtures.
@MainActor private final class OnboardingHTTP: HTTPTransport {
    var state: CompanionCore.State
    var view: [String: Any]
    var requests: [URLRequest] = []
    var receipts: [String: Data] = [:]
    var loseNextReply = false
    var finishRecords = 0
    var offline = false
    var reject: String?
    var hold = false
    var entered = false
    var continuation: CheckedContinuation<Void, Never>?
    init(legacy: Bool = false) throws {
        state = try fixture()
        view = ["schema_version": "1", "data_epoch": state.data_epoch, "revision": 0,
                "status": legacy ? "complete" : "active", "current_step": legacy ? "complete" : "health", "legacy": legacy,
                "steps": ["health", "bank", "notifications"].map { ["id": $0, "title": $0, "purpose": "Optional context", "optional": true, "decision": "pending"] as [String: Any] },
                "initial_sync": ["status": legacy ? "deferred" : "pending", "available_records": NSNull()]]
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        if offline { throw URLError(.notConnectedToInternet) }
        if request.url?.path == "/state" { return HTTPResult(status: 200, body: try Wire.encode(state)) }
        guard ["/onboarding/v1", "/onboarding/v1/actions"].contains(request.url?.path ?? "") else { throw ClientFailure.invalidEndpoint }
        if let reject, request.httpMethod == "POST" {
            return HTTPResult(status: 409, body: try JSONSerialization.data(withJSONObject: ["error": ["code": reject, "retryable": false]]))
        }
        var response: Data
        if request.httpMethod == "POST" {
            let body = try #require(JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any])
            let id = body["request_id"] as! String
            if let saved = receipts[id] { response = saved }
            else {
                #expect(body["expected_revision"] as? Int == view["revision"] as? Int)
                #expect(body["step"] as? String == view["current_step"] as? String)
                #expect(body["data_epoch"] as? String == state.data_epoch)
                #expect(Set(body.keys) == ["schema_version", "request_id", "data_epoch", "expected_revision", "step", "action"])
                let action = body["action"] as! String, step = body["step"] as! String
                if step == "initial_sync" {
                    view["status"] = "complete"; view["current_step"] = "complete"
                    view["initial_sync"] = ["status": action == "defer" ? "deferred" : (finishRecords > 0 ? "records_available" : "no_data"), "available_records": action == "defer" ? NSNull() : finishRecords] as [String: Any]
                } else {
                    var steps = view["steps"] as! [[String: Any]]
                    let index = steps.firstIndex { $0["id"] as? String == step }!
                    steps[index]["decision"] = action; view["steps"] = steps
                    view["current_step"] = index == 2 ? "initial_sync" : steps[index + 1]["id"]
                }
                view["revision"] = (view["revision"] as! Int) + 1
                response = try JSONSerialization.data(withJSONObject: view); receipts[id] = response
            }
            if loseNextReply { loseNextReply = false; throw URLError(.networkConnectionLost) }
        } else { response = try JSONSerialization.data(withJSONObject: view) }
        if hold { entered = true; await withCheckedContinuation { continuation = $0 } }
        return HTTPResult(status: 200, body: response)
    }
}

@Test @MainActor func onboardingNewAccountGatesAndSkipDoesNotTriggerDeviceOrUpload() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client)
    #expect(!controller.canShowMain)
    await controller.load(); #expect(controller.overview?.current_step == .health)
    await controller.decide(.skip); #expect(controller.overview?.current_step == .bank)
    await controller.decide(.skip); await controller.decide(.skip)
    #expect(controller.overview?.current_step == .initial_sync && !controller.canShowMain)
    await controller.decide(.finish)
    #expect(!controller.canShowMain && controller.completionSummary?.status == .no_data)
    #expect(controller.completionSummary?.available_records == 0)
    controller.acknowledgeCompletion(); #expect(controller.canShowMain)
    #expect(client.snapshot.outbox.isEmpty)
    #expect(http.requests.allSatisfy { ["/state", "/onboarding/v1", "/onboarding/v1/actions"].contains($0.url!.path) })
    let count = http.requests.count; await controller.decide(.skip)
    #expect(http.requests.count == count)
}

@Test @MainActor func onboardingLegacyCompleteBypassesWithoutNewDecisionsAndReloadResumes() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(legacy: true), client = try CompanionClient(storage: storage, transport: http)
    let first = OnboardingController(client: client); await first.load()
    #expect(first.canShowMain && first.overview?.legacy == true)
    let second = OnboardingController(client: client); await second.load()
    #expect(second.canShowMain)
    #expect(http.requests.allSatisfy { $0.httpMethod != "POST" })
}

@Test @MainActor func onboardingLostReplyRetryUsesIdenticalBytesAndReloadReconciles() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load()
    http.loseNextReply = true; await controller.decide(.continue)
    let original = http.requests.last!.httpBody
    #expect(controller.hasRetry && controller.overview?.current_step == .health)
    await controller.decide(.skip); #expect(http.requests.last!.httpBody == original)
    await controller.retryDecision()
    #expect(http.requests.last!.httpBody == original)
    #expect(!controller.hasRetry && controller.overview?.current_step == .bank)
    http.loseNextReply = true; await controller.decide(.unavailable); #expect(controller.hasRetry)
    await controller.load(); #expect(!controller.hasRetry && controller.overview?.current_step == .notifications)
}

@Test @MainActor func onboardingDeniedUnavailableDeferredAreDecisionsNotPermissionClaims() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load()
    var deviceCalls = 0
    await controller.runDeviceAction { deviceCalls += 1; return "No supported records readable; authorization unknown." }
    #expect(controller.overview?.current_step == .health && controller.overview?.decision(for: .health) == .pending)
    #expect(deviceCalls == 1 && controller.deviceMessage?.contains("unknown") == true)
    await controller.decide(.denied); await controller.decide(.unavailable); await controller.decide(.unavailable)
    await controller.decide(.defer)
    #expect(!controller.canShowMain && controller.completionSummary?.status == .deferred)
    #expect(controller.completionSummary?.available_records == nil)
    controller.acknowledgeCompletion(); #expect(controller.canShowMain)
}

@Test @MainActor func onboardingFailedLoadRemainsGatedAndDoesNotPersistProgress() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); http.offline = true
    await controller.load(); #expect(controller.overview == nil && controller.issue != nil && !controller.canShowMain)
    http.offline = false; await controller.load(); await controller.decide(.skip)
    let restarted = OnboardingController(client: client); #expect(restarted.overview == nil && !restarted.canShowMain)
    await restarted.load(); #expect(restarted.overview?.current_step == .bank)
    let disk = String(data: try Data(contentsOf: storage.directory.appendingPathComponent("snapshot.json")), encoding: .utf8)!
    #expect(!disk.contains("onboarding") && !disk.contains("Optional context"))
}

@Test @MainActor func onboardingConflictDoesNotRetagAndMalformedResponseFailsClosed() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load()
    http.reject = "stale_onboarding"; await controller.decide(.skip)
    #expect(controller.overview == nil && !controller.hasRetry && !controller.canShowMain)
    let count = http.requests.count; await controller.retryDecision(); #expect(http.requests.count == count)
    http.reject = nil; http.view["current_step"] = "complete"
    await controller.load(); #expect(controller.overview == nil && !controller.canShowMain)
}

@Test @MainActor func onboardingLateLoadCannotResurrectSignedOutUI() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(legacy: true), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); http.hold = true
    let work = Task { await controller.load() }
    for _ in 0..<100 where !http.entered { await Task.yield() }
    #expect(http.entered)
    controller.suspend(); client.invalidateSession(); http.continuation?.resume(); await work.value
    #expect(controller.overview == nil && controller.deviceMessage == nil && !controller.canShowMain && !controller.busy)
}

@Test @MainActor func onboardingLateMutationCannotResurrectOrClobberResumedUI() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load(); http.hold = true
    let work = Task { await controller.decide(.skip) }
    for _ in 0..<100 where !http.entered { await Task.yield() }
    #expect(http.entered)
    controller.suspend(); controller.resume(); http.hold = false
    await controller.load(); #expect(controller.overview?.current_step == .bank)
    http.continuation?.resume(); await work.value
    #expect(controller.overview?.current_step == .bank && !controller.busy && !controller.hasRetry)
}

@Test @MainActor func onboardingEpochChangeFencesDeviceCallbacksAndOldActions() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load()
    let old = try OnboardingActionRequest(view: #require(controller.overview), action: .skip)
    await controller.runDeviceAction {
        http.state.data_epoch = "new-account-epoch"
        try? await client.refresh()
        return "PRIVATE OLD RESULT"
    }
    #expect(controller.overview == nil && controller.deviceMessage == nil)
    await #expect(throws: ClientFailure.rejected("epoch_mismatch", false)) { try await client.onboarding(old) }
    #expect(!http.requests.contains { $0.httpMethod == "POST" })
}

@Test @MainActor func onboardingRefusesOlderRevisionAndCrossEpochGET() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load(); await controller.decide(.skip)
    http.view["revision"] = 0; await controller.load()
    #expect(controller.overview == nil && !controller.canShowMain)
    http.view["data_epoch"] = "other-account"; await controller.load()
    #expect(controller.overview == nil && controller.issue != nil)
}

@Test @MainActor func onboardingRecordsSummaryIsAcknowledgedOnceAndLaterSignInBypasses() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    http.finishRecords = 7
    let controller = OnboardingController(client: client); await controller.load()
    await controller.decide(.continue); await controller.decide(.skip); await controller.decide(.skip)
    await controller.decide(.finish)
    #expect(!controller.canShowMain && controller.completionSummary?.status == .records_available)
    #expect(controller.completionSummary?.available_records == 7)
    await controller.load(); #expect(controller.completionSummary?.available_records == 7)
    controller.acknowledgeCompletion(); #expect(controller.canShowMain && controller.completionSummary == nil)
    let laterSignIn = OnboardingController(client: client); await laterSignIn.load()
    #expect(laterSignIn.canShowMain && laterSignIn.completionSummary == nil)
}

@Test @MainActor func onboardingLostFinishReplyReloadShowsSummaryBeforeOpeningOverview() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load()
    for _ in 0..<3 { await controller.decide(.skip) }
    http.loseNextReply = true; await controller.decide(.finish)
    #expect(controller.hasRetry && controller.completionSummary == nil && !controller.canShowMain)
    await controller.load()
    #expect(!controller.hasRetry && controller.completionSummary?.status == .no_data && !controller.canShowMain)
    controller.acknowledgeCompletion(); #expect(controller.canShowMain)
}

@Test @MainActor func onboardingLostFinishReplyRetryShowsSummaryAndSuspensionPurgesIt() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load()
    for _ in 0..<3 { await controller.decide(.skip) }
    http.loseNextReply = true; await controller.decide(.defer)
    let bytes = http.requests.last!.httpBody
    await controller.retryDecision()
    #expect(http.requests.last!.httpBody == bytes)
    #expect(controller.completionSummary?.status == .deferred && !controller.canShowMain)
    controller.suspend(); #expect(controller.completionSummary == nil && controller.overview == nil)
    controller.resume(); await controller.load()
    #expect(controller.canShowMain && controller.completionSummary == nil)
}

@Test @MainActor func onboardingFailedFinishCanExplicitlyDeferAfterReconciliation() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load()
    for _ in 0..<3 { await controller.decide(.skip) }
    http.reject = "internal_error"; await controller.decide(.finish)
    #expect(controller.hasRetry && controller.overview?.current_step == .initial_sync)
    http.reject = nil
    await controller.finishWithoutWaiting()
    #expect(!controller.hasRetry && controller.completionSummary?.status == .deferred)
    #expect(!controller.canShowMain)
    controller.acknowledgeCompletion(); #expect(controller.canShowMain)
}

@Test @MainActor func onboardingDeferralDoesNotOverrideAlreadyCommittedLostFinish() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client); await controller.load()
    for _ in 0..<3 { await controller.decide(.skip) }
    http.loseNextReply = true; await controller.decide(.finish)
    let postCount = http.requests.filter { $0.httpMethod == "POST" }.count
    await controller.finishWithoutWaiting()
    #expect(http.requests.filter { $0.httpMethod == "POST" }.count == postCount)
    #expect(controller.completionSummary?.status == .no_data && !controller.hasRetry)
}

@Test @MainActor func onboardingAppModelPurgesOnAdmissionBackgroundAndSignout() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(legacy: true), client = try CompanionClient(storage: storage, transport: http)
    let model = AppModel(client: client, accountNamespace: "onboarding-local-test")
    await model.onboarding.load(); #expect(model.onboarding.canShowMain)
    model.hideForAdmission(); #expect(model.onboarding.overview == nil)
    model.allowAdmittedUpdates(); await model.onboarding.load(); #expect(model.onboarding.canShowMain)
    model.suspendForegroundForBackground(); #expect(model.onboarding.overview == nil)
    model.allowAdmittedUpdates(); await model.onboarding.load()
    model.invalidateForSignOut(); #expect(model.onboarding.overview == nil && !model.onboarding.canShowMain)
}

@Test @MainActor func onboardingConcurrentFinishConflictStillShowsReconciledOutcome() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try OnboardingHTTP(), client = try CompanionClient(storage: storage, transport: http)
    let controller = OnboardingController(client: client)
    await controller.load()
    for _ in 0..<3 { await controller.decide(.skip) }
    http.reject = "onboarding_complete"
    await controller.decide(.finish)
    #expect(!controller.hasRetry && controller.issue != nil)
    // Another authenticated request completed while our finish was in flight.
    http.view["status"] = "complete"; http.view["current_step"] = "complete"
    http.view["revision"] = 4
    http.view["initial_sync"] = ["status": "no_data", "available_records": 0] as [String: Any]
    http.reject = nil
    await controller.load()
    #expect(controller.completionSummary?.status == .no_data && !controller.canShowMain)
    controller.acknowledgeCompletion()
    #expect(controller.canShowMain)
}
