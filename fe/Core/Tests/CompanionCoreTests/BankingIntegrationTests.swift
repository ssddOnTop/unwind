#if os(macOS)
import Foundation
import Testing
@testable import CompanionCore

@MainActor private final class BankTokens: SessionTokenProvider {
    var currentIdentity: AuthIdentity? = AuthIdentity(issuer: "https://banking-tests.example", subject: "test-user-A", sessionID: "test-session-A")
    var refreshes = 0
    func token(for identity: AuthIdentity, forceRefresh: Bool) async throws -> String? {
        if forceRefresh { refreshes += 1 }
        return refreshes > 0 ? "unit-refreshed" : "unit-token"
    }
}
@MainActor private final class BankService: HTTPTransport {
    var state: CompanionCore.State
    var inventory: [String: Any]
    var requests: [URLRequest] = []
    var mutationBodies: [Data] = []
    var receipts: [String: Data] = [:]
    var profileMissing = false
    var unauthorizedOnce = false
    var reject: String?
    var failures = 0
    var hold = false
    var entered = false
    var pending: CheckedContinuation<Void, Never>?
    var syncStatus = "complete"
    var commits = 0
    var expiredLink = false
    var bankingRevisionOffset = 0
    init() throws {
        let file = Bundle.module.url(forResource: "banking-inventory.example", withExtension: "json", subdirectory: "Fixtures")!
        inventory = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as! [String: Any]
        inventory["result"] = NSNull()
        state = try fixture(); state.data_epoch = inventory["data_epoch"] as! String; state.revision = inventory["revision"] as! Int
    }
    func release() { pending?.resume(); pending = nil }
    func bytes(_ object: Any) throws -> Data { try JSONSerialization.data(withJSONObject: object, options: .sortedKeys) }
    func overview(result: Any = NSNull()) throws -> Data {
        var data = inventory; data["result"] = result; data["revision"] = state.revision + bankingRevisionOffset; data["data_epoch"] = state.data_epoch
        return try bytes(data)
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        switch request.url!.path {
        case "/auth/v1/session": return HTTPResult(status: 200, body: try bytes(["schema_version": "1", "issuer": "https://banking-tests.example", "subject": "test-user-A"]), contentType: "application/json")
        case "/state": return HTTPResult(status: 200, body: try Wire.encode(state))
        case "/experience/v1":
            let file = Bundle.module.url(forResource: "experience.example", withExtension: "json", subdirectory: "Fixtures")!
            var value = try Wire.decode(ExperienceV1.self, Data(contentsOf: file)); value.data_epoch = state.data_epoch; value.revision = state.revision
            return HTTPResult(status: 200, body: try Wire.encode(value))
        case "/banking/v1":
            if profileMissing { return HTTPResult(status: 404, body: try bytes(["detail": "Not Found"])) }
            return HTTPResult(status: 200, body: try overview())
        case "/banking/v1/actions": break
        default: throw ClientFailure.invalidEndpoint // Never permit a legacy/provider shortcut in tests.
        }
        let body = request.httpBody!; mutationBodies.append(body)
        if unauthorizedOnce { unauthorizedOnce = false; return HTTPResult(status: 401, body: Data()) }
        if hold { entered = true; await withCheckedContinuation { pending = $0 } }
        if failures > 0 { failures -= 1; throw URLError(.networkConnectionLost) }
        let value = try JSONSerialization.jsonObject(with: body) as! [String: Any]
        let id = value["request_id"] as! String
        if let receipt = receipts[id] { return HTTPResult(status: 200, body: receipt) }
        if let code = reject ?? ((value["expected_revision"] as? Int) == state.revision ? nil : "stale_banking") {
            return HTTPResult(status: 409, body: try bytes(["error": ["code": code, "message": "Test server rejection", "retryable": code == "busy", "request_id": id, "details": []]]))
        }
        let action = value["action"] as! String
        var result: [String: Any] = ["status": "saved", "message": "Server confirmed change", "link_session_id": NSNull(), "link_token": NSNull(), "expiration": NSNull(), "added": NSNull(), "modified": NSNull(), "removed": NSNull()]
        var connections = inventory["connections"] as! [[String: Any]]
        switch action {
        case "add_link", "update_link":
            result["status"] = "link_ready"; result["link_session_id"] = "unit-link-session"
            result["link_token"] = "link-sandbox-unit-test-only"
            result["expiration"] = expiredLink ? "2001-01-01T00:00:00Z" : "2099-01-01T00:00:00Z"
        case "exchange":
            var new = connections[0]; new["id"] = "connection-new"; new["label"] = "New server bank"
            var accounts = new["accounts"] as! [[String: Any]]
            accounts = accounts.enumerated().map { i, old in var a = old; a["id"] = "new-account-\(i)"; a["enabled"] = false; a["import_status"] = "disabled"; return a }
            new["accounts"] = accounts; new["groups"] = []; connections.append(new)
        case "reconcile":
            let index = connections.firstIndex { $0["id"] as? String == value["connection_id"] as? String }!
            var accounts = connections[index]["accounts"] as! [[String: Any]]
            var discovered = accounts[0]; discovered["id"] = "newly-discovered"; discovered["enabled"] = false; discovered["mask"] = "0013"; discovered["import_status"] = "disabled"; accounts.append(discovered)
            connections[index]["accounts"] = accounts
        case "select_accounts", "select_group":
            let index = connections.firstIndex { $0["id"] as? String == value["connection_id"] as? String }!
            let ids = value["account_ids"] as! [String]
            connections[index]["accounts"] = (connections[index]["accounts"] as! [[String: Any]]).map { old in
                var account = old
                if ids.contains(old["id"] as! String) { account["enabled"] = value["enabled"]!; account["import_status"] = (value["enabled"] as! Bool) ? "not_synced" : "disabled" }
                return account
            }
        case "sync":
            result["status"] = syncStatus; result["message"] = syncStatus == "partial" ? "Server reports imports still partial" : "Server reports sync complete"
            result["added"] = 0; result["modified"] = 0; result["removed"] = 0
        case "cancel_link": result["status"] = "cancelled"; result["message"] = "Server cancelled pending setup"
        case "disconnect":
            let index = connections.firstIndex { $0["id"] as? String == value["connection_id"] as? String }!
            connections[index]["status"] = "disconnected"; connections[index]["can_manage"] = false
            connections[index]["accounts"] = (connections[index]["accounts"] as! [[String: Any]]).map { old in var a = old; a["enabled"] = false; a["availability"] = "disconnected"; a["can_enable"] = false; a["import_status"] = "disabled"; return a }
            result["status"] = "disconnected"
        default: throw ClientFailure.invalidResponse
        }
        inventory["connections"] = connections; state.revision += 1; commits += 1
        let response = try overview(result: result); receipts[id] = response
        return HTTPResult(status: 200, body: response)
    }
}
@MainActor private final class BankRig {
    let service: BankService
    let tokens = BankTokens(), fence = AuthSessionFence()
    let storage = temporaryStorage()
    var transport: AuthenticatedTransport!
    var client: CompanionClient!
    var model: AppModel!
    var controller: BankingController { model.banking }
    init() throws { service = try BankService() }
    func prepare() async throws {
        let url = URL(string: "http://127.0.0.1:9876")!
        transport = try AuthenticatedTransport(base: service, provider: tokens, fence: fence, lease: fence.activate(tokens.currentIdentity!), origin: url)
        try await transport.admit(); let t = transport!
        client = try CompanionClient(storage: storage, transport: t, baseURL: url, allowedPort: 9876, retryDelay: 0, sessionValidator: { try t.validate() })
        model = AppModel(client: client, accountNamespace: "test-bank-account")
        let host = model!
        transport.onAdmissionInvalidated = { [weak host] in host?.hideForAdmission() }
        transport.onAdmissionRestored = { [weak host] in host?.allowAdmittedUpdates() }
        fence.onLock = { [weak host] in host?.invalidateForSignOut() }
        await model.refresh()
    }
    func event(_ action: BankingAction) throws -> BankingEvent { BankingEvent(action: action, version: try #require(controller.overview).version) }
    func wait(_ condition: () -> Bool) async throws {
        for _ in 0..<30_000 { if condition() { return }; await Task.yield() }
        throw ClientFailure.invalidResponse
    }
    func cleanup() { service.release(); model?.invalidateForSignOut(); try? FileManager.default.removeItem(at: storage.directory) }
    func actions() throws -> [[String: Any]] { try service.mutationBodies.map { try JSONSerialization.jsonObject(with: $0) as! [String: Any] } }
}

@Test @MainActor func integratedBankingRefreshesAfterAdmissionWithoutLegacyFallback() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    #expect(rig.model.connected); #expect(rig.model.plaid == nil)
    #expect(rig.controller.overview?.connections.count == 2)
    #expect(rig.controller.overview?.connections[0].accounts[0].mask == "0007")
    #expect(rig.service.requests.first?.url?.path == "/auth/v1/session")
    #expect(!rig.service.requests.contains { $0.url?.path == "/connections/plaid" })
}
@Test @MainActor func integratedMissingBankingProfileIsNotEmptyInventoryOrAnonymousFallback() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; rig.service.profileMissing = true; try await rig.prepare()
    #expect(rig.model.connected); #expect(rig.controller.overview == nil)
    guard case .failed(let message) = rig.controller.status else { Issue.record("Expected explicit profile setup error"); return }
    #expect(message.contains("banking profile")); #expect(rig.service.mutationBodies.isEmpty)
    #expect(!rig.service.requests.contains { $0.url?.path == "/connections/plaid" || $0.url?.path == "/demo" })
}
@Test @MainActor func integratedEmptyBankingResponseStillOffersAuthoritativeEmptyOverview() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; rig.service.inventory["connections"] = []; rig.service.inventory["status"] = "not_connected"; try await rig.prepare()
    #expect(rig.controller.status == .idle); #expect(rig.controller.overview?.connections.isEmpty == true)
}
@Test @MainActor func integratedSelectionWaitsForServerAndPreservesExactGroupIDs() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    let original = try #require(rig.controller.overview); let bank = original.connections[0], group = bank.groups[0]
    rig.service.hold = true
    rig.controller.perform(try rig.event(.selectGroup(connectionID: bank.id, groupID: group.id, accountIDs: group.account_ids, enabled: false)), requestID: "group-stable")
    try await rig.wait { rig.service.entered }
    #expect(rig.controller.overview == original); #expect(rig.controller.status == .updating)
    rig.service.release(); try await rig.wait { !rig.controller.working }
    #expect(rig.controller.overview?.connections[0].accounts.allSatisfy { !$0.enabled } == true)
    #expect(rig.controller.overview?.connections[1] == original.connections[1])
    #expect(rig.client.snapshot.state?.observations == rig.service.state.observations)
    let request = try #require(rig.actions().first)
    #expect(request["group_id"] as? String == group.id); #expect(request["account_ids"] as? [String] == group.account_ids)
    #expect(request["request_id"] as? String == "group-stable")
}
@Test @MainActor func integratedSafeTransportRetryPreservesBytesAndLogicalIdentity() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare(); rig.service.failures = 1
    rig.controller.perform(try rig.event(.sync(connectionID: nil)), requestID: "sync-stable")
    try await rig.wait { rig.service.mutationBodies.count == 2 && !rig.controller.working }
    #expect(rig.service.mutationBodies[0] == rig.service.mutationBodies[1]); #expect(rig.service.commits == 1)
    #expect(try rig.actions()[0]["connection_id"] is NSNull)
    #expect(rig.controller.notice == "Server reports sync complete")
}
@Test @MainActor func integratedBanking401RetryRemainsVisibleAndPartialIsNotComplete() async throws {
    for partial in [false, true] {
        let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
        rig.service.unauthorizedOnce = true; rig.service.syncStatus = partial ? "partial" : "complete"
        rig.controller.perform(try rig.event(.sync(connectionID: "connection-opaque")), requestID: "sync-auth")
        try await rig.wait { rig.service.commits == 1 && !rig.controller.working }
        #expect(rig.tokens.refreshes == 1); #expect(rig.service.mutationBodies.count == 2)
        #expect(rig.service.mutationBodies[0] == rig.service.mutationBodies[1])
        #expect(rig.controller.status == .idle)
        #expect(rig.controller.notice == (partial ? "Server reports imports still partial" : "Server reports sync complete"))
    }
}
@Test @MainActor func integratedAddNativeSuccessExchangesOnceAndNewAccountsStayDisabled() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    rig.controller.perform(try rig.event(.addLink))
    try await rig.wait { rig.controller.session != nil }
    let callback = try #require(rig.controller.session).configuration
    callback.onLoad(); try await rig.wait { rig.controller.showingLink }
    callback.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-banking"))
    try await rig.wait { rig.service.commits == 2 && !rig.controller.working }
    let actions = try rig.actions(); #expect(actions.map { $0["action"] as! String } == ["add_link", "exchange"])
    #expect(actions[1]["link_session_id"] as? String == "unit-link-session")
    #expect(rig.controller.overview?.connections.count == 3)
    #expect(rig.controller.overview?.connections.last?.accounts.allSatisfy { !$0.enabled } == true)
    callback.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-banking")); callback.onLoad()
    for _ in 0..<30 { await Task.yield() }
    #expect(rig.service.commits == 2); #expect(!rig.controller.showingLink)
}
@Test @MainActor func integratedUpdateNativeSuccessReconcilesAndNeverExchangesCallbackToken() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    let bank = try #require(rig.controller.overview?.connections.first)
    rig.controller.perform(try rig.event(.updateLink(connectionID: bank.id)))
    try await rig.wait { rig.controller.session != nil }
    let callback = try #require(rig.controller.session).configuration
    callback.onSuccess(LinkSuccess(publicToken: "must-not-be-read-or-sent"))
    try await rig.wait { rig.service.commits == 2 && !rig.controller.working }
    let actions = try rig.actions(); #expect(actions.map { $0["action"] as! String } == ["update_link", "reconcile"])
    #expect(actions[1]["public_token"] == nil); #expect(actions[1]["connection_id"] as? String == bank.id)
    #expect(rig.controller.overview?.connections[0].accounts.last?.enabled == false)
    #expect(rig.controller.overview?.connections[0].accounts[1].enabled == bank.accounts[1].enabled)
}
@Test @MainActor func integratedNativeExitCancelsOnlyPendingSessionWithoutDisconnect() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    let original = rig.controller.overview?.connections
    rig.controller.perform(try rig.event(.updateLink(connectionID: "connection-opaque")))
    try await rig.wait { rig.controller.session != nil }
    let callback = try #require(rig.controller.session).configuration
    callback.onExit(LinkExit(error: nil))
    try await rig.wait { rig.service.commits == 2 && !rig.controller.working }
    #expect(try rig.actions().map { $0["action"] as! String } == ["update_link", "cancel_link"])
    #expect(rig.controller.overview?.connections == original)
    #expect(rig.controller.notice == "Server cancelled pending setup")
}
@Test @MainActor func integratedStaleSelectionsRefreshButNeverRetagOrReplay() async throws {
    for code in ["stale_banking", "obsolete_selection"] {
        let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
        let event = try rig.event(.selectAccounts(connectionID: "connection-opaque", accountIDs: ["account-opaque"], enabled: true))
        rig.service.reject = code; rig.service.state.revision += 1
        rig.controller.perform(event, requestID: "stale-original")
        try await rig.wait { rig.service.mutationBodies.count == 1 && !rig.controller.working }
        guard case .failed = rig.controller.status else { Issue.record("A stale action must require another user choice"); continue }
        #expect(rig.controller.overview?.revision == rig.service.state.revision)
        let body = try rig.actions()[0]; #expect(body["expected_revision"] as? Int == event.version.revision)
        #expect(body["request_id"] as? String == "stale-original"); #expect(rig.service.mutationBodies.count == 1)
    }
}
@Test @MainActor func integratedAmbiguousExchangeIsNotReplayedOrPersisted() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    rig.controller.perform(try rig.event(.addLink)); try await rig.wait { rig.controller.session != nil }
    let callback = try #require(rig.controller.session).configuration; rig.service.failures = 1
    callback.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-banking"))
    try await rig.wait { rig.service.mutationBodies.count == 2 && !rig.controller.working }
    #expect(rig.controller.notice?.contains("not replayed") == true)
    #expect(rig.service.commits == 1); #expect(rig.client.snapshot.outbox.isEmpty)
    let saved = try String(contentsOf: rig.storage.directory.appendingPathComponent("snapshot.json"), encoding: .utf8)
    for forbidden in ["public-sandbox", "link-sandbox", "Server institution", "account-opaque", "Server checking"] { #expect(!saved.contains(forbidden)) }
}
@Test @MainActor func integratedDisconnectKeepsHistoryAndOtherBankUntouched() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    let other = rig.controller.overview?.connections[1], history = rig.client.snapshot.state?.observations
    rig.controller.perform(try rig.event(.disconnect(connectionID: "connection-opaque")))
    try await rig.wait { rig.service.commits == 1 && !rig.controller.working }
    #expect(rig.controller.overview?.connections[0].status == .disconnected)
    #expect(rig.controller.overview?.connections[0].accounts.isEmpty == false)
    #expect(rig.controller.overview?.connections[1] == other)
    #expect(rig.client.snapshot.state?.observations == history)
}
@Test @MainActor func integratedAccountSwitchAndLogoutDiscardDelayedBankResponses() async throws {
    for switching in [false, true] {
        let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare(); rig.service.hold = true
        rig.controller.perform(try rig.event(.sync(connectionID: nil))); try await rig.wait { rig.service.entered }
        rig.fence.lock()
        if switching {
            rig.tokens.currentIdentity = AuthIdentity(issuer: "https://banking-tests.example", subject: "test-user-B", sessionID: "test-session-B")
            _ = rig.fence.activate(rig.tokens.currentIdentity!)
        } else { rig.tokens.currentIdentity = nil }
        rig.service.release(); try await rig.wait { rig.service.commits == 1 }
        for _ in 0..<30 { await Task.yield() }
        #expect(rig.controller.overview == nil); #expect(rig.controller.session == nil)
        #expect(rig.client.bankingSnapshot == nil); #expect(rig.model.state == nil); #expect(!rig.controller.working)
    }
}
@Test @MainActor func integratedEpochRotationRejectsDelayedSelectionAndOldLinkCallback() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    rig.controller.perform(try rig.event(.addLink)); try await rig.wait { rig.controller.session != nil }
    let old = try #require(rig.controller.session).configuration
    rig.service.state.data_epoch = "new-bank-epoch"; await rig.model.refresh()
    old.onLoad(); old.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-banking"))
    for _ in 0..<30 { await Task.yield() }
    #expect(rig.service.mutationBodies.count == 1); #expect(!rig.controller.showingLink)
    #expect(rig.controller.overview?.data_epoch != "banking-example-epoch")
}
@Test @MainActor func integratedExpiredLinkDoesNotOpenNativeUI() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare(); rig.service.expiredLink = true
    rig.controller.perform(try rig.event(.addLink)); try await rig.wait { rig.service.commits == 1 && !rig.controller.working }
    #expect(rig.controller.session == nil); #expect(!rig.controller.showingLink)
    guard case .failed = rig.controller.status else { Issue.record("Expired Link must fail closed"); return }
}
@Test func bankingRequestsCannotEnterDurableOutbox() throws {
    let version = BankingVersion(data_epoch: "banking-example-epoch", revision: 12)
    for action: BankingAction in [.addLink, .exchange(linkSessionID: "pending-unit", publicToken: "public-sandbox-unit"), .sync(connectionID: nil), .selectAccounts(connectionID: "connection-opaque", accountIDs: ["account-opaque"], enabled: true)] {
        #expect(throws: ClientFailure.rejected("invalid_request", false)) {
            try PendingMutation(path: "/banking/v1/actions", request: BankingRequest(action: action, version: version, requestID: "unit"))
        }
    }
}

@Test @MainActor func integratedIndividualSelectionPublishesOnlyCommittedState() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    rig.controller.perform(try rig.event(.selectAccounts(connectionID: "connection-opaque", accountIDs: ["account-opaque"], enabled: true)), requestID: "enable-single")
    try await rig.wait { rig.service.commits == 1 && !rig.controller.working }
    #expect(rig.controller.overview?.connections[0].accounts[0].enabled == true)
    #expect(rig.controller.overview?.revision == rig.client.snapshot.state?.revision)
    #expect(try rig.actions()[0]["action"] as? String == "select_accounts")
}
@Test @MainActor func integratedBankingRejectsUncommittedOrStaleOverviewRevision() async throws {
    for offset in [-1, 5] {
        let rig = try BankRig(); defer { rig.cleanup() }; rig.service.bankingRevisionOffset = offset; try await rig.prepare()
        #expect(rig.controller.overview == nil); #expect(rig.client.bankingSnapshot == nil)
        guard case .failed = rig.controller.status else { Issue.record("Mismatched State/banking revisions must fail closed"); continue }
        #expect(rig.service.mutationBodies.isEmpty)
    }
}
@Test @MainActor func integratedAdmissionRejectsOldNativeCallbacksAndDoesNotReviveLink() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    rig.controller.perform(try rig.event(.addLink)); try await rig.wait { rig.controller.session != nil }
    let old = try #require(rig.controller.session).configuration
    rig.transport.invalidateAdmission(); try await rig.transport.admit(); await rig.model.refresh()
    old.onLoad(); old.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-banking")); old.onExit(LinkExit(error: nil))
    for _ in 0..<30 { await Task.yield() }
    #expect(rig.service.mutationBodies.count == 1)
    #expect(rig.controller.session == nil); #expect(!rig.controller.showingLink)
}
@Test @MainActor func integratedLifecycleCancellationDiscardsDelayedInventorySelection() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare(); rig.service.hold = true
    rig.controller.perform(try rig.event(.selectAccounts(connectionID: "connection-opaque", accountIDs: ["account-opaque"], enabled: true)))
    try await rig.wait { rig.service.entered }; rig.controller.cancelForLifecycle(); rig.service.release()
    try await rig.wait { rig.service.commits == 1 }
    for _ in 0..<30 { await Task.yield() }
    #expect(rig.controller.overview == nil); #expect(rig.controller.notice == nil); #expect(!rig.controller.working)
    #expect(rig.client.bankingSnapshot == nil)
}
@Test @MainActor func integratedStaleNativeSuccessCannotRetagRevisionAndExchangeAgain() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    rig.controller.perform(try rig.event(.addLink)); try await rig.wait { rig.controller.session != nil }
    let old = try #require(rig.controller.session).configuration
    let originalVersion = try #require(rig.controller.overview).version
    rig.service.state.revision += 1
    old.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-banking"))
    try await rig.wait { rig.service.mutationBodies.count == 2 && !rig.controller.working }
    let sent = try rig.actions()[1]
    #expect(sent["expected_revision"] as? Int == originalVersion.revision)
    #expect(rig.controller.overview?.revision == rig.service.state.revision)
    #expect(rig.service.commits == 1)
    guard case .failed = rig.controller.status else { Issue.record("Stale exchange needs explicit recovery, not retagging"); return }
}
@Test func bankingEncodedActionsMatchFrozenSchema() throws {
    let version = BankingVersion(data_epoch: "banking-example-epoch", revision: 12)
    let actions: [BankingAction] = [.addLink, .exchange(linkSessionID: "unit-session", publicToken: "public-sandbox-test-only"), .updateLink(connectionID: "connection-opaque"), .reconcile(connectionID: "connection-opaque", linkSessionID: "unit-session"), .cancelLink(linkSessionID: "unit-session"), .selectAccounts(connectionID: "connection-opaque", accountIDs: ["account-opaque"], enabled: false), .selectGroup(connectionID: "connection-opaque", groupID: "group-opaque", accountIDs: ["account-other", "account-opaque"], enabled: true), .sync(connectionID: nil), .sync(connectionID: "connection-opaque"), .disconnect(connectionID: "connection-opaque")]
    let values = try actions.enumerated().map { index, action in try JSONSerialization.jsonObject(with: BankingRequest(action: action, version: version, requestID: "schema-case-\(index)").body()) }
    let target = URL(fileURLWithPath: ProcessInfo.processInfo.environment["COMPANION_EVIDENCE_DIR"]!).appendingPathComponent("encoded-banking-actions.json")
    try JSONSerialization.data(withJSONObject: values, options: [.prettyPrinted, .sortedKeys]).write(to: target)
    #expect(values.count == 10)
}

@Test @MainActor func integratedBusyRetriesAreBoundedAndKeepTheExactRequest() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare(); rig.service.reject = "busy"
    rig.controller.perform(try rig.event(.sync(connectionID: nil)), requestID: "bounded-busy")
    try await rig.wait { rig.service.mutationBodies.count == 3 && !rig.controller.working }
    #expect(rig.service.commits == 0)
    #expect(rig.service.mutationBodies.allSatisfy { $0 == rig.service.mutationBodies[0] })
    guard case .failed = rig.controller.status else { Issue.record("Busy exhaustion must be explicit"); return }
}
@Test @MainActor func integratedExchange401MayRetryOnlyThroughSameSessionReadmission() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    rig.controller.perform(try rig.event(.addLink)); try await rig.wait { rig.controller.session != nil }
    let callback = try #require(rig.controller.session).configuration; rig.service.unauthorizedOnce = true
    callback.onSuccess(LinkSuccess(publicToken: "public-sandbox-unit-banking"))
    try await rig.wait { rig.service.commits == 2 && !rig.controller.working }
    #expect(rig.tokens.refreshes == 1); #expect(rig.service.mutationBodies.count == 3)
    #expect(rig.service.mutationBodies[1] == rig.service.mutationBodies[2])
    #expect(rig.controller.overview?.connections.count == 3)
    #expect(rig.controller.status == .idle)
}

@Test @MainActor func integratedPendingDeletionCannotRevealInventoryOrReplayThroughBankRefresh() async throws {
    let rig = try BankRig(); defer { rig.cleanup() }; try await rig.prepare()
    let reads = rig.service.requests.filter { $0.url?.path == "/banking/v1" }.count
    let deletion = DeleteRequest(schema_version: "1", request_id: "pending-delete", data_epoch: rig.service.state.data_epoch, confirm: "delete_all")
    try rig.client.enqueue(PendingMutation(path: "/data", method: "DELETE", request: deletion))
    await rig.controller.refresh()
    #expect(rig.controller.overview == nil); #expect(rig.client.bankingSnapshot == nil)
    #expect(rig.service.requests.filter { $0.url?.path == "/banking/v1" }.count == reads)
    #expect(rig.client.snapshot.outbox.count == 1); #expect(rig.service.mutationBodies.isEmpty)
    guard case .failed(let message) = rig.controller.status else { Issue.record("Pending deletion must gate inventory"); return }
    #expect(message.contains("Your data"))
}
#endif
