#if os(macOS)
import Foundation
import Testing
import Combine
@testable import CompanionCore

@MainActor private final class FlowTokens: SessionTokenProvider {
    var currentIdentity: AuthIdentity? = AuthIdentity(issuer: "https://controller-tests.example", subject: "unit-user", sessionID: "unit-session")
    var refreshes = 0
    func token(for identity: AuthIdentity, forceRefresh: Bool) async throws -> String? {
        if forceRefresh { refreshes += 1 }
        return refreshes > 0 ? "test-refreshed" : "test-token"
    }
}
@MainActor private final class FlowHTTP: HTTPTransport {
    var state: CompanionCore.State
    var unauthorizedOnce = false
    var unauthorizedAction = "sync"
    var unauthorizedAlways = false
    var unauthorizedValidationOnce = false
    var responseStatus = "complete"
    var domainError: String?
    var hold = false
    var held: CheckedContinuation<Void, Never>?
    var enteredHold = false
    var calls: [URLRequest] = []
    var confirmed = 0
    init() throws {
        let data = try Data(contentsOf: Bundle.module.url(forResource: "plaid-examples", withExtension: "json", subdirectory: "Fixtures")!)
        let examples = try JSONSerialization.jsonObject(with: data) as! [[String: Any]]
        state = try Wire.decode(CompanionCore.State.self, JSONSerialization.data(withJSONObject: examples[5]["state"]!))
    }
    func release() { held?.resume(); held = nil }
    var syncCalls: [URLRequest] { calls.filter { $0.httpMethod == "POST" && (try? Wire.decode(PlaidSyncRequest.self, $0.httpBody!)) != nil } }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        calls.append(request)
        if request.url?.path == "/auth/v1/session" {
            return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: ["schema_version": "1", "issuer": "https://controller-tests.example", "subject": "unit-user"]), contentType: "application/json")
        }
        if request.url?.path == "/state" {
            if confirmed > 0 && unauthorizedValidationOnce { unauthorizedValidationOnce = false; return HTTPResult(status: 401, body: Data()) }
            return HTTPResult(status: 200, body: try Wire.encode(state))
        }
        if request.url?.path == "/experience/v1" {
            let file = Bundle.module.url(forResource: "experience.example", withExtension: "json", subdirectory: "Fixtures")!
            var value = try Wire.decode(ExperienceV1.self, Data(contentsOf: file)); value.data_epoch = state.data_epoch; value.revision = state.revision
            return HTTPResult(status: 200, body: try Wire.encode(value))
        }
        let object = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
        let action = object["action"] as! String
        if action == unauthorizedAction, unauthorizedOnce || unauthorizedAlways { unauthorizedOnce = false; return HTTPResult(status: 401, body: Data()) }
        if hold { enteredHold = true; await withCheckedContinuation { held = $0 } }
        if let domainError {
            return HTTPResult(status: 409, body: try Wire.encode(APIErrorEnvelope(error: APIErrorEnvelopeError(code: domainError, message: "Test-only rejection", retryable: false, request_id: object["request_id"] as? String, details: []))))
        }
        let result: [String: Any]
        if action == "link_token" { result = ["link_token": "link-sandbox-unit-only", "expiration": "2099-01-01T00:00:00Z"] }
        else if action == "exchange" { result = ["status": "connected"] }
        else if action == "disconnect" { result = ["status": "disconnected"] }
        else { result = ["status": responseStatus, "added": 2, "modified": 0, "removed": 0, "skipped": 0, "retry_after_seconds": NSNull()] }
        state.revision += 1; confirmed += 1
        let body: [String: Any] = ["schema_version": "1", "request_id": object["request_id"]!, "data_epoch": object["data_epoch"]!, "action": action, "environment": "sandbox", "result": result, "state": try dictionary(state)]
        return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: body))
    }
}
@MainActor private final class FlowHarness {
    let network: FlowHTTP
    let tokens = FlowTokens()
    let fence = AuthSessionFence()
    let storage = temporaryStorage()
    var transport: AuthenticatedTransport!
    var client: CompanionClient!
    var model: AppModel!
    var controller: PlaidController { model.plaid! }
    init() throws { network = try FlowHTTP() }
    func prepare() async throws {
        let origin = URL(string: "http://127.0.0.1:9876")!
        transport = try AuthenticatedTransport(base: network, provider: tokens, fence: fence, lease: fence.activate(tokens.currentIdentity!), origin: origin)
        try await transport.admit()
        let authenticated = transport!
        client = try CompanionClient(storage: storage, transport: authenticated, baseURL: origin, allowedPort: 9876, retryDelay: 0, sessionValidator: { try authenticated.validate() })
        model = AppModel(client: client, accountNamespace: "test-only")
        let host = model!
        transport.onAdmissionInvalidated = { [weak host] in host?.hideForAdmission() }
        transport.onAdmissionRestored = { [weak host] in host?.allowAdmittedUpdates() }
        fence.onLock = { [weak host] in host?.invalidateForSignOut() }
        await host.refresh()
        #expect(host.connected)
    }
    func cleanup() { network.release(); model?.invalidateForSignOut(); try? FileManager.default.removeItem(at: storage.directory) }
    func wait(_ condition: () -> Bool) async throws {
        for _ in 0..<20_000 { if condition() { return }; await Task.yield() }
        throw ClientFailure.invalidResponse
    }
}

@Test @MainActor func actualControllerNormalSyncPublishesConfirmedResult() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    var forwardedChanges = 0
    let observation = flow.model.objectWillChange.sink { forwardedChanges += 1 }
    defer { observation.cancel() }
    flow.controller.sync()
    try await flow.wait { flow.network.confirmed == 1 && !flow.controller.working && forwardedChanges > 0 }
    #expect(flow.controller.lastSync?.status == "complete")
    #expect(flow.controller.lastSync?.added == 2)
    #expect(flow.controller.lastIssue == nil)
    #expect(flow.network.syncCalls.count == 1)
}

@Test @MainActor func actualController401ReadmissionPreservesVisibleExactRetrySuccess() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.network.unauthorizedOnce = true
    flow.controller.sync()
    try await flow.wait { flow.network.confirmed == 1 && flow.client.snapshot.state?.revision == flow.network.state.revision && flow.model.connected && !flow.controller.working }
    #expect(flow.tokens.refreshes == 1)
    #expect(flow.network.syncCalls.count == 2)
    #expect(flow.network.syncCalls[0].httpBody == flow.network.syncCalls[1].httpBody)
    #expect(flow.network.syncCalls[0].url == flow.network.syncCalls[1].url)
    #expect(flow.network.syncCalls[0].value(forHTTPHeaderField: "Authorization") != flow.network.syncCalls[1].value(forHTTPHeaderField: "Authorization"))
    #expect(flow.controller.lastSync?.status == "complete", "The actual AppModel admission callback must not silently discard the confirmed result")
    #expect(flow.controller.lastSync?.added == 2)
    #expect(flow.controller.lastIssue == nil)
}

@Test @MainActor func actualControllerReadmissionDuringStateValidationKeepsResult() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.network.unauthorizedValidationOnce = true; flow.controller.sync()
    try await flow.wait { flow.network.confirmed == 1 && !flow.controller.working }
    #expect(flow.tokens.refreshes == 1); #expect(flow.network.syncCalls.count == 1)
    #expect(flow.controller.lastSync?.status == "complete")
    #expect(flow.controller.updateNotice?.contains("confirmed") == true)
}

@Test @MainActor func actualControllerReadmittedWaitingAndPartialStayPendingWithoutNewPoll() async throws {
    for status in ["waiting", "partial"] {
        let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
        flow.network.unauthorizedOnce = true; flow.network.responseStatus = status; flow.controller.sync()
        try await flow.wait { flow.network.confirmed == 1 && !flow.controller.working }
        #expect(flow.controller.lastSync?.status == status)
        #expect(flow.controller.updateNotice?.contains("still pending") == true)
        #expect(flow.controller.lastIssue == nil)
        #expect(flow.network.syncCalls.count == 2)
        #expect(flow.network.syncCalls[0].httpBody == flow.network.syncCalls[1].httpBody)
    }
}

@Test @MainActor func actualControllerNormalWaitingAndPartialRemainVisibleUntilCancellation() async throws {
    for status in ["waiting", "partial"] {
        let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
        flow.network.responseStatus = status; flow.controller.sync()
        try await flow.wait { flow.controller.lastSync?.status == status }
        #expect(flow.controller.working); #expect(flow.network.syncCalls.count == 1)
        flow.controller.cancel("Update stopped. Import readiness remains unconfirmed.")
        #expect(!flow.controller.working); #expect(flow.controller.lastSync == nil)
        #expect(flow.controller.updateNotice == "Update stopped. Import readiness remains unconfirmed.")
    }
}

@Test @MainActor func actualControllerErrorResultsAndRejectionsAreTerminalAndVisible() async throws {
    for code in ["result_error", "consent_required", "not_found"] {
        let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
        if code == "result_error" { flow.network.responseStatus = "error" }
        else { flow.network.domainError = code }
        flow.controller.sync()
        try await flow.wait { !flow.network.syncCalls.isEmpty && !flow.controller.working }
        #expect(flow.controller.lastIssue?.isEmpty == false)
        #expect(flow.controller.updateNotice?.isEmpty == false)
        #expect(flow.controller.lastSync?.status != "complete")
        #expect(flow.network.syncCalls.count == 1)
    }
}

@Test @MainActor func actualControllerCancellationRejectsDelayedCompletion() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.network.hold = true; flow.controller.sync()
    try await flow.wait { flow.network.enteredHold }
    flow.controller.cancel("Update interrupted. The server outcome is unconfirmed.")
    flow.network.release()
    try await flow.wait { flow.network.confirmed == 1 }
    for _ in 0..<20 { await Task.yield() }
    #expect(!flow.controller.working); #expect(flow.controller.lastSync == nil)
    #expect(flow.controller.updateNotice == "Update interrupted. The server outcome is unconfirmed.")
}

@Test @MainActor func actualControllerLogoutAndSwitchDiscardLateResults() async throws {
    for switching in [false, true] {
        let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
        flow.network.hold = true; flow.controller.sync(); try await flow.wait { flow.network.enteredHold }
        flow.fence.lock()
        if switching {
            flow.tokens.currentIdentity = AuthIdentity(issuer: "https://controller-tests.example", subject: "other-unit-user", sessionID: "other-unit-session")
            _ = flow.fence.activate(flow.tokens.currentIdentity!)
        } else { flow.tokens.currentIdentity = nil }
        flow.network.release(); try await flow.wait { flow.network.confirmed == 1 }
        for _ in 0..<20 { await Task.yield() }
        #expect(flow.model.state == nil); #expect(flow.client.snapshot.state == nil)
        #expect(flow.controller.lastSync == nil); #expect(!flow.controller.working)
        #expect(flow.network.syncCalls.count == 1)
    }
}

@Test @MainActor func actualControllerEpochRotationRejectsOldSyncResult() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.network.hold = true; flow.controller.sync(); try await flow.wait { flow.network.enteredHold }
    flow.network.state.data_epoch = "rotated-unit-epoch"
    await flow.model.refresh()
    flow.network.release(); try await flow.wait { flow.network.confirmed == 1 }
    for _ in 0..<20 { await Task.yield() }
    #expect(flow.model.state?.data_epoch == "rotated-unit-epoch")
    #expect(flow.controller.lastSync == nil); #expect(!flow.controller.working)
    #expect(flow.controller.updateNotice?.isEmpty == false)
}

@Test @MainActor func actualControllerSecond401LocksWithoutStuckWorkingOrFalseSuccess() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.network.unauthorizedAlways = true; flow.controller.sync()
    try await flow.wait { flow.network.syncCalls.count == 2 && !flow.controller.working }
    #expect(flow.tokens.refreshes == 1); #expect(flow.fence.identity == nil)
    #expect(flow.model.state == nil); #expect(flow.controller.lastSync == nil)
    #expect(flow.controller.updateNotice?.isEmpty == false)
}

@Test @MainActor func actualControllerOldLinkCallbacksCannotReviveAfterReadmission() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.controller.connect(); try await flow.wait { flow.controller.session != nil }
    let old = try #require(flow.controller.session).configuration
    old.onLoad(); try await flow.wait { flow.controller.showingLink }
    flow.transport.invalidateAdmission()
    #expect(!flow.controller.showingLink); #expect(flow.controller.session == nil)
    #expect(!flow.controller.working); #expect(flow.controller.updateNotice?.isEmpty == false)
    try await flow.transport.admit(); await flow.model.refresh()
    let before = flow.network.calls.filter { $0.httpMethod == "POST" }.count
    old.onLoad(); old.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-only")); old.onExit(LinkExit(error: nil))
    for _ in 0..<30 { await Task.yield() }
    #expect(flow.network.calls.filter { $0.httpMethod == "POST" }.count == before)
    #expect(!flow.controller.showingLink); #expect(flow.controller.session == nil)
    #expect(!flow.controller.working)
}

@Test @MainActor func actualControllerAdmissionDuringPollingSleepIsExplicitlyInterrupted() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.network.responseStatus = "waiting"; flow.controller.sync()
    try await flow.wait { flow.controller.lastSync?.status == "waiting" }
    flow.transport.invalidateAdmission()
    #expect(!flow.controller.working); #expect(flow.controller.lastSync == nil)
    #expect(flow.controller.updateNotice?.contains("interrupted") == true)
    try await flow.transport.admit(); await flow.model.refresh()
    for _ in 0..<30 { await Task.yield() }
    #expect(flow.network.syncCalls.count == 1)
    #expect(flow.controller.updateNotice?.contains("interrupted") == true)
}

@Test @MainActor func actualControllerCannotStartWithoutForegroundReportsReason() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.controller.setForeground(false); flow.controller.sync()
    #expect(!flow.controller.working); #expect(flow.network.syncCalls.isEmpty)
    #expect(flow.controller.updateNotice?.contains("could not start") == true)
}

@Test @MainActor func actualControllerReadmittedLinkTokenNeverRecreatesInvalidatedSDKFlow() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.network.unauthorizedOnce = true; flow.network.unauthorizedAction = "link_token"
    flow.controller.connect()
    try await flow.wait { flow.network.confirmed == 1 && !flow.controller.working }
    #expect(flow.tokens.refreshes == 1)
    #expect(flow.controller.session == nil); #expect(!flow.controller.showingLink)
    #expect(flow.controller.updateNotice?.contains("interrupted") == true)
    let sent = flow.network.calls.filter { $0.httpMethod == "POST" }
    #expect(sent.count == 2); #expect(sent[0].httpBody == sent[1].httpBody)
}

@Test @MainActor func actualControllerCompletedLinkCallbacksCannotReopenOrEraseResult() async throws {
    let flow = try FlowHarness(); defer { flow.cleanup() }; try await flow.prepare()
    flow.controller.connect(); try await flow.wait { flow.controller.session != nil }
    let old = try #require(flow.controller.session).configuration
    old.onLoad(); try await flow.wait { flow.controller.showingLink }
    old.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-only"))
    try await flow.wait { flow.controller.lastSync?.status == "complete" && !flow.controller.working }
    let before = flow.network.calls.filter { $0.httpMethod == "POST" }.count
    old.onLoad(); old.onExit(LinkExit(error: nil)); old.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-only"))
    for _ in 0..<30 { await Task.yield() }
    #expect(!flow.controller.showingLink); #expect(flow.controller.session == nil)
    #expect(flow.controller.lastSync?.status == "complete"); #expect(!flow.controller.working)
    #expect(flow.network.calls.filter { $0.httpMethod == "POST" }.count == before)
}
#endif
