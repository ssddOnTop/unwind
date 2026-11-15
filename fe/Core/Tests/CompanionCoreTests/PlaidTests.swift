import Foundation
import Testing
@testable import CompanionCore

private func examples() throws -> [[String: Any]] {
    let url = try #require(Bundle.module.url(forResource: "plaid-examples", withExtension: "json", subdirectory: "Fixtures"))
    return try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [[String: Any]])
}
private func bytes(_ object: Any) throws -> Data { try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) }
private func bankState() throws -> State {
    var state = try Wire.decode(State.self, bytes(examples()[5]["state"]!))
    var observation = try #require(fixture().observations.first(where: { $0.kind == "purchase" }))
    observation.source_id = "plaid"; observation.household_attribution = "unknown"
    observation.mode = "fixture"; observation.time_precision = "date"; observation.occurred_at = nil
    observation.provenance.adapter = "plaid_sandbox"; observation.provenance.method = "transactions_sync"
    observation.provenance.adapter_version = "1"
    state.observations = [observation]
    return state
}
private func waiting(_ status: String = "waiting", hint: Int? = 2) -> PlaidSyncResponseResult {
    PlaidSyncResponseResult(status: status, added: 0, modified: 0, removed: 0, skipped: 0, retry_after_seconds: hint)
}

@Test func plaidAllFrozenRequestAndResponseExamples() throws {
    let all = try examples()
    let token = try #require(all[2]["public_token"] as? String)
    let actions: [PlaidAction] = [.linkToken, .exchange(publicToken: token), .sync, .disconnect]
    for (i, action) in actions.enumerated() {
        let request = all[i * 2], response = all[i * 2 + 1]
        let id = request["request_id"] as! String, epoch = request["data_epoch"] as! String
        let encoded = try action.body(epoch: epoch, requestID: id)
        #expect(try bytes(JSONSerialization.jsonObject(with: encoded)) == bytes(request))
        let reply = try PlaidReply.decode(bytes(response), action: action, requestID: id, epoch: epoch, now: PlaidReply.date("2026-11-14T10:00:00Z")!)
        #expect(reply.epoch == epoch); #expect(reply.requestID == id)
        #expect(try dictionary(reply.state).keys.count == (response["state"] as! [String: Any]).keys.count)
    }
}
@Test func plaidExplicitNullsAndCountOnlyUnknownAttribution() throws {
    let state = try bankState()
    let observation = try #require(state.observations.first)
    #expect(observation.mode == "fixture"); #expect(observation.household_attribution == "unknown")
    #expect(observation.provenance.adapter == "plaid_sandbox"); #expect(observation.provenance.method == "transactions_sync")
    let encoded = try dictionary(observation)
    #expect(encoded["occurred_at"] is NSNull); #expect(encoded["activity"] is NSNull)
    let purchase = try #require(encoded["purchase"] as? [String: Any])
    #expect(Set(purchase.keys) == Set(["category", "merchant", "authorized_date", "posted_date", "is_refund"]))
    #expect(try dictionary(waiting("complete", hint: nil))["retry_after_seconds"] is NSNull)
}
@Test func plaidRejectsUnknownFieldsAtEveryResponseLayer() throws {
    var original = try examples()[5]; original["state"] = try dictionary(bankState())
    for layer in ["root", "result", "state", "provenance", "purchase"] {
        var value = original
        if layer == "root" { value["unexpected"] = true }
        else if layer == "result" { var result = value["result"] as! [String: Any]; result["unexpected"] = true; value["result"] = result }
        else {
            var state = value["state"] as! [String: Any]
            if layer == "state" { state["unexpected"] = true }
            else {
                var observations = state["observations"] as! [[String: Any]]
                var nested = observations[0][layer] as! [String: Any]; nested["unexpected"] = true
                observations[0][layer] = nested; state["observations"] = observations
            }
            value["state"] = state
        }
        #expect(throws: (any Error).self) { try Wire.decode(PlaidSyncResponse.self, bytes(value)) }
    }
}
@Test func plaidRejectsWrongEnvironmentAndMissingNull() throws {
    var value = try examples()[5]; value["environment"] = "production"
    #expect(throws: (any Error).self) { try Wire.decode(PlaidSyncResponse.self, bytes(value)) }
    value = try examples()[5]; var result = value["result"] as! [String: Any]
    result.removeValue(forKey: "retry_after_seconds"); value["result"] = result
    #expect(throws: (any Error).self) { try Wire.decode(PlaidSyncResponse.self, bytes(value)) }
}
@Test func plaidRejectsInvalidProvenanceAndLiveMode() throws {
    let original = try dictionary(bankState().observations[0])
    for (field, bad) in [("adapter", "future_unknown_adapter"), ("method", "provider_record"), ("adapter_version", "2"), ("mode", "live"), ("source_id", "healthkit")] {
        var value = original
        if field == "mode" || field == "source_id" { value[field] = bad }
        else { var provenance = value["provenance"] as! [String: Any]; provenance[field] = bad; value["provenance"] = provenance }
        #expect(throws: (any Error).self) { try Wire.decode(Observation.self, bytes(value)) }
    }
}
@Test func plaidRejectsExpiredTokensWrongIdentityAndCounts() throws {
    let all = try examples(), link = all[1], sync = all[5]
    #expect(throws: PlaidFailure.expiredLinkToken) {
        try PlaidReply.decode(bytes(link), action: .linkToken, requestID: "plaid-1", epoch: "sandbox-epoch-1", now: PlaidReply.date("2026-11-15T00:00:00Z")!)
    }
    #expect(throws: PlaidFailure.staleWork) { try PlaidReply.decode(bytes(sync), action: .sync, requestID: "wrong", epoch: "sandbox-epoch-1") }
    #expect(throws: PlaidFailure.staleWork) { try PlaidReply.decode(bytes(sync), action: .sync, requestID: "plaid-3", epoch: "new-epoch") }
    var invalid = sync; var result = sync["result"] as! [String: Any]; result["added"] = -1; invalid["result"] = result
    #expect(throws: PlaidFailure.invalidSandboxResponse) { try PlaidReply.decode(bytes(invalid), action: .sync, requestID: "plaid-3", epoch: "sandbox-epoch-1") }
    #expect(throws: PlaidFailure.invalidSandboxResponse) { try PlaidAction.exchange(publicToken: "public-production-no").body(epoch: "e", requestID: "r") }
}
@Test func plaidPollingIsBoundedForegroundAndCancellable() {
    var plan = PlaidPollPlan()
    #expect((0..<5).compactMap { _ in plan.nextDelay(result: waiting(), foreground: true, connected: true) } == [2, 4, 8, 16, 30])
    #expect(plan.nextDelay(result: waiting(), foreground: true, connected: true) == nil)
    plan = PlaidPollPlan(); #expect(plan.nextDelay(result: waiting(hint: 12), foreground: true, connected: true) == 12)
    plan.cancel(); #expect(plan.nextDelay(result: waiting(), foreground: true, connected: true) == nil)
    for result in [waiting("complete", hint: nil), waiting("error", hint: nil)] {
        var stopped = PlaidPollPlan(); #expect(stopped.nextDelay(result: result, foreground: true, connected: true) == nil)
    }
    var paused = PlaidPollPlan()
    #expect(paused.nextDelay(result: waiting(), foreground: false, connected: true) == nil)
    #expect(paused.nextDelay(result: waiting(), foreground: true, connected: false) == nil)
    #expect(paused.attempts == 0)
}

private actor BankHTTP: HTTPTransport {
    var state: State
    var requests: [Data] = []
    var receipts: [String: Data] = [:]
    var commits = 0
    var failures: Int
    let rejection: String?
    let rotate: Bool
    let revoke: Bool
    let held: Bool
    var pending: CheckedContinuation<Void, Never>?
    var observer: CheckedContinuation<Void, Never>?
    init(state: State, failures: Int = 0, rejection: String? = nil, rotate: Bool = false, revoke: Bool = false, held: Bool = false) {
        self.state = state; self.failures = failures; self.rejection = rejection; self.rotate = rotate; self.revoke = revoke; self.held = held
    }
    func waitForRequest() async {
        if requests.isEmpty { await withCheckedContinuation { observer = $0 } }
    }
    func release() { pending?.resume(); pending = nil }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        if request.httpMethod == "GET" { return HTTPResult(status: 200, body: try Wire.encode(state)) }
        let body = request.httpBody!; requests.append(body)
        if held { await withCheckedContinuation { pending = $0; observer?.resume(); observer = nil } }
        if failures > 0 { failures -= 1; throw URLError(.networkConnectionLost) }
        let input = try JSONSerialization.jsonObject(with: body) as! [String: Any]
        if let rejection {
            return HTTPResult(status: rejection == "internal_error" ? 500 : 409, body: try Wire.encode(APIErrorEnvelope(error: APIErrorEnvelopeError(code: rejection, message: "Synthetic rejection", retryable: rejection == "internal_error", request_id: (input["request_id"] as! String), details: []))))
        }
        let id = input["request_id"] as! String
        if let receipt = receipts[id] { return HTTPResult(status: 200, body: receipt) }
        let action = input["action"] as! String
        let result: [String: Any]
        switch action {
        case "exchange": result = ["status": "connected"]
        case "link_token": result = ["link_token": "link-sandbox-synthetic-test-only", "expiration": "2099-01-01T00:00:00Z"]
        case "disconnect": result = ["status": "removal_pending"]
        default: result = ["status": "complete", "added": 0, "modified": 0, "removed": 0, "skipped": 0, "retry_after_seconds": NSNull()]
        }
        state.revision += 1
        let reply = try bytes(["schema_version": "1", "request_id": input["request_id"]!, "data_epoch": state.data_epoch, "action": action, "environment": "sandbox", "result": result, "state": dictionary(state)])
        receipts[id] = reply; commits += 1
        if rotate { state.data_epoch = "rotated-epoch"; state.observations = []; state.feedback = [] }
        if revoke {
            state.revision += 1; state.preferences.enabled_source_ids = []
            for i in state.sources.indices where state.sources[i].id == "plaid" { state.sources[i].enabled = false; state.sources[i].consent = "revoked" }
        }
        return HTTPResult(status: 200, body: reply)
    }
}
@Test @MainActor func plaidRetriesExactBytesButNewPollGetsNewID() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let transport = BankHTTP(state: try bankState(), failures: 1)
    let client = try CompanionClient(storage: storage, transport: transport, retryDelay: 0)
    try await client.refresh(); let epoch = client.snapshot.state!.data_epoch
    _ = try await client.plaid(.sync, epoch: epoch)
    _ = try await client.plaid(.sync, epoch: epoch)
    let bodies = await transport.requests
    #expect(bodies.count == 3); #expect(bodies[0] == bodies[1]); #expect(bodies[1] != bodies[2])
    #expect(client.snapshot.outbox.isEmpty)
}
@Test @MainActor func plaidAmbiguousExchangeNeverAutomaticallyReplayedOrPersisted() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let transport = BankHTTP(state: try bankState(), failures: 2)
    let client = try CompanionClient(storage: storage, transport: transport, retryDelay: 0)
    try await client.refresh()
    await #expect(throws: PlaidFailure.ambiguousExchange) { try await client.plaid(.exchange(publicToken: "public-sandbox-synthetic-test-only"), epoch: client.snapshot.state!.data_epoch) }
    #expect(await transport.requests.count == 1)
    let disk = try String(contentsOf: storage.directory.appendingPathComponent("snapshot.json"), encoding: .utf8)
    #expect(!disk.contains("public-sandbox")); #expect(!disk.contains("public_token")); #expect(client.snapshot.outbox.isEmpty)
}
@Test @MainActor func plaidLinkTokenOnlyInMemoryNotSnapshot() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let client = try CompanionClient(storage: storage, transport: BankHTTP(state: try bankState()))
    try await client.refresh(); _ = try await client.plaid(.linkToken, epoch: client.snapshot.state!.data_epoch)
    let disk = try String(contentsOf: storage.directory.appendingPathComponent("snapshot.json"), encoding: .utf8)
    #expect(!disk.contains("link-sandbox")); #expect(!disk.contains("link_token")); #expect(client.snapshot.outbox.isEmpty)
}
@Test @MainActor func plaidOldEpochRejectedBeforeTransport() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let transport = BankHTTP(state: try bankState())
    let client = try CompanionClient(storage: storage, transport: transport)
    try await client.refresh()
    await #expect(throws: PlaidFailure.staleWork) { try await client.plaid(.exchange(publicToken: "public-sandbox-synthetic-test-only"), epoch: "old-epoch") }
    #expect(await transport.requests.isEmpty)
}
@Test @MainActor func plaidResetBetweenReceiptAndRefreshRejectsToken() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let client = try CompanionClient(storage: storage, transport: BankHTTP(state: try bankState(), rotate: true))
    try await client.refresh(); let epoch = client.snapshot.state!.data_epoch
    await #expect(throws: PlaidFailure.staleWork) { try await client.plaid(.linkToken, epoch: epoch) }
    #expect(client.snapshot.state?.data_epoch == "rotated-epoch"); #expect(client.snapshot.state?.observations.isEmpty == true)
}
@Test @MainActor func plaidRevokeBetweenReceiptAndRefreshRejectsToken() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let client = try CompanionClient(storage: storage, transport: BankHTTP(state: try bankState(), revoke: true))
    try await client.refresh(); let epoch = client.snapshot.state!.data_epoch
    await #expect(throws: PlaidFailure.staleWork) { try await client.plaid(.linkToken, epoch: epoch) }
    await #expect(throws: ClientFailure.rejected("consent_required", false)) { try await client.plaid(.sync, epoch: epoch) }
}
@Test @MainActor func plaidCancellationRejectsDelayedLinkExchangeAndSync() async throws {
    for action: PlaidAction in [.linkToken, .exchange(publicToken: "public-sandbox-synthetic-test-only"), .sync] {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let transport = BankHTTP(state: try bankState(), held: true)
        let client = try CompanionClient(storage: storage, transport: transport)
        try await client.refresh(); let before = client.snapshot
        let task = Task { try await client.plaid(action, epoch: before.state!.data_epoch) }
        await transport.waitForRequest(); client.invalidatePlaidWork(); await transport.release()
        await #expect(throws: PlaidFailure.staleWork) { try await task.value }
        #expect(client.snapshot == before)
    }
}
@Test @MainActor func plaidUnavailableAndDisconnectPendingRemainTruthful() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let bad = BankHTTP(state: try bankState(), rejection: "not_configured")
    let client = try CompanionClient(storage: storage, transport: bad)
    try await client.refresh(); let before = client.snapshot
    await #expect(throws: ClientFailure.rejected("not_configured", false)) { try await client.plaid(.linkToken, epoch: before.state!.data_epoch) }
    #expect(client.snapshot == before); #expect(await bad.requests.count == 1)
    let other = try CompanionClient(storage: storage, transport: BankHTTP(state: try bankState(), revoke: true))
    #expect(other.snapshot.state == nil) // Relaunch must fetch backend state, never restore a read cache.
    try await other.refresh()
    let reply = try await other.plaid(.disconnect, epoch: before.state!.data_epoch)
    #expect(reply.result == .disconnected("removal_pending")); #expect(other.snapshot.state?.preferences.enabled_source_ids.isEmpty == true)
}

@Test func plaidExportsActualEncodedProfileCases() throws {
    let all = try examples()
    var cases: [[String: Any]] = []
    func add<T: Encodable>(_ definition: String, _ value: T) throws {
        cases.append(["definition": definition, "payload": try dictionary(value)])
    }
    try add("link_token_request", Wire.decode(PlaidLinkTokenRequest.self, bytes(all[0])))
    try add("link_token_response", Wire.decode(PlaidLinkTokenResponse.self, bytes(all[1])))
    try add("exchange_request", Wire.decode(PlaidExchangeRequest.self, bytes(all[2])))
    try add("exchange_response", Wire.decode(PlaidExchangeResponse.self, bytes(all[3])))
    try add("sync_request", Wire.decode(PlaidSyncRequest.self, bytes(all[4])))
    try add("sync_response", Wire.decode(PlaidSyncResponse.self, bytes(all[5])))
    try add("disconnect_request", Wire.decode(PlaidDisconnectRequest.self, bytes(all[6])))
    try add("disconnect_response", Wire.decode(PlaidDisconnectResponse.self, bytes(all[7])))
    try add("state", bankState())
    let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let evidence = ProcessInfo.processInfo.environment["COMPANION_EVIDENCE_DIR"].map { URL(fileURLWithPath: $0) } ?? root.appendingPathComponent("evidence/plaid")
    try FileManager.default.createDirectory(at: evidence, withIntermediateDirectories: true)
    try bytes(cases).write(to: evidence.appendingPathComponent("encoded-plaid-contracts.json"), options: .atomic)
    #expect(cases.count == 9)
}

@Test @MainActor func plaidSuccessfulExchangeReceiptReplayUsesSameIdentity() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let transport = BankHTTP(state: try bankState())
    let client = try CompanionClient(storage: storage, transport: transport)
    try await client.refresh(); let epoch = client.snapshot.state!.data_epoch
    let first = try await client.plaid(.exchange(publicToken: "public-sandbox-synthetic-test-only"), epoch: epoch, requestID: "identical-exchange")
    let second = try await client.plaid(.exchange(publicToken: "public-sandbox-synthetic-test-only"), epoch: epoch, requestID: "identical-exchange")
    #expect(first == second); #expect(await transport.commits == 1)
    let sent = await transport.requests; #expect(sent.count == 2); #expect(sent[0] == sent[1])
}
@Test @MainActor func plaidAmbiguousServerExchangeFailureDoesNotRetry() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let transport = BankHTTP(state: try bankState(), rejection: "internal_error")
    let client = try CompanionClient(storage: storage, transport: transport, retryDelay: 0)
    try await client.refresh()
    await #expect(throws: PlaidFailure.ambiguousExchange) { try await client.plaid(.exchange(publicToken: "public-sandbox-synthetic-test-only"), epoch: client.snapshot.state!.data_epoch) }
    #expect(await transport.requests.count == 1)
}
@Test func plaidConnectionRequestsCannotEnterDiskOutbox() throws {
    let request = PlaidExchangeRequest(schema_version: "1", request_id: "test", data_epoch: "epoch", action: "exchange", public_token: "public-sandbox-synthetic-test-only")
    #expect(throws: ClientFailure.rejected("invalid_request", false)) { try PendingMutation(path: "/connections/plaid", request: request) }
}
