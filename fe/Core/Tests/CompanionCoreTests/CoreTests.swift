import Foundation
import Testing
@testable import CompanionCore

func fixture() throws -> State {
    try Wire.decode(State.self, Data(contentsOf: Bundle.module.url(forResource: "state.example", withExtension: "json", subdirectory: "Fixtures")!))
}
func batchFixture() throws -> ObservationBatch {
    try Wire.decode(ObservationBatch.self, Data(contentsOf: Bundle.module.url(forResource: "observation-batch.example", withExtension: "json", subdirectory: "Fixtures")!))
}
func dictionary<T: Encodable>(_ value: T) throws -> [String: Any] {
    try #require(JSONSerialization.jsonObject(with: Wire.encode(value)) as? [String: Any])
}
func temporaryStorage() -> ProtectedStorage {
    ProtectedStorage(directory: URL(fileURLWithPath: ProcessInfo.processInfo.environment["TMPDIR"]!).appendingPathComponent(UUID().uuidString))
}
func feedback(epoch: String = "demo-epoch-1") -> FeedbackRequest {
    FeedbackRequest(schema_version: "1", request_id: "feedback-test", data_epoch: epoch, card_id: "purchase_recurrence", kind: "correctness", value: "partial", scope: "card", event_ref: nil, attribution: nil, note: "")
}

actor StubHTTP: HTTPTransport {
    var state: State
    var requests: [URLRequest] = []
    var failures: Int
    var rejection: String?
    var rotate: Bool
    init(state: State, failures: Int = 0, rejection: String? = nil, rotate: Bool = false) { self.state = state; self.failures = failures; self.rejection = rejection; self.rotate = rotate }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        if request.httpMethod == "GET" { return HTTPResult(status: 200, body: try Wire.encode(state)) }
        if failures > 0 { failures -= 1; throw URLError(.networkConnectionLost) }
        if let rejection {
            let envelope = APIErrorEnvelope(error: APIErrorEnvelopeError(code: rejection, message: "Safe message", retryable: rejection == "busy", request_id: "feedback-test", details: []))
            return HTTPResult(status: 409, body: try Wire.encode(envelope))
        }
        state.revision += 1
        if rotate { state.data_epoch = "new-epoch"; state.observations = []; state.notification_intents = []; state.feedback = [] }
        if request.url?.path == "/observations" {
            return HTTPResult(status: 200, body: try Wire.encode(IngestionResponse(schema_version: "1", accepted: 0, ignored: 0, state: state)))
        }
        return HTTPResult(status: 200, body: try Wire.encode(state))
    }
    func mutations() -> [URLRequest] { requests.filter { $0.httpMethod != "GET" } }
}

@Test func fixtureRoundTripIsExact() throws {
    let original = try Data(contentsOf: Bundle.module.url(forResource: "state.example", withExtension: "json", subdirectory: "Fixtures")!)
    let object = try JSONSerialization.jsonObject(with: original) as! NSDictionary
    #expect(try dictionary(fixture()) as NSDictionary == object)
}
@Test func fixtureEvidenceAndUncertainty() throws {
    let s = try fixture()
    #expect(s.ledger.first { $0.id == "recorded_steps" }?.recorded_steps == 13800)
    #expect(s.ledger.first { $0.id == "recorded_steps" }?.days_with_records == 3)
    #expect(s.ledger.first { $0.id == "purchase_recurrence" }?.purchase_count == 3)
    #expect(s.preferences.movement_restriction)
    #expect(s.notification_intents.isEmpty)
    for card in s.ledger {
        for ref in card.evidence { #expect(s.observations.contains { $0.id == ref.observation_id && $0.revision == ref.revision }) }
        #expect(!card.limitations.isEmpty)
    }
}
@Test func batchRoundTripIncludesNulls() throws {
    let b = try batchFixture()
    #expect(b.observations.count == 6)
    let object = try dictionary(b.observations[0])
    #expect(object["activity"] is NSNull)
    #expect(object["occurred_at"] is NSNull)
    #expect(object["supersedes"] is NSNull)
    #expect(try dictionary(b.source_updates[0])["error_code"] is NSNull)
}
@Test func feedbackNullsNotOmitted() throws {
    let f = try dictionary(feedback())
    #expect(f["event_ref"] is NSNull); #expect(f["attribution"] is NSNull)
    #expect(Set(f.keys) == Set(["schema_version","request_id","data_epoch","card_id","kind","value","scope","event_ref","attribution","note"]))
}
@Test func missingNullableIsRejected() throws {
    var d = try dictionary(feedback()); d.removeValue(forKey: "event_ref")
    let data = try JSONSerialization.data(withJSONObject: d)
    #expect(throws: (any Error).self) { try Wire.decode(FeedbackRequest.self, data) }
}
@Test func unknownFieldsAreRejected() throws {
    var d = try dictionary(feedback()); d["raw_record"] = "forbidden"
    let data = try JSONSerialization.data(withJSONObject: d)
    #expect(throws: (any Error).self) { try Wire.decode(FeedbackRequest.self, data) }
}
@Test func unsupportedVersionRejected() throws {
    var d = try dictionary(feedback()); d["schema_version"] = "2"
    let data = try JSONSerialization.data(withJSONObject: d)
    #expect(throws: (any Error).self) { try Wire.decode(FeedbackRequest.self, data) }
}
@Test func invalidEnumRejected() throws {
    var d = try dictionary(feedback()); d["value"] = "yes"
    let data = try JSONSerialization.data(withJSONObject: d)
    #expect(throws: (any Error).self) { try Wire.decode(FeedbackRequest.self, data) }
    var p = try fixture().preferences; p.enabled_source_ids = ["unexpected"]
    #expect(throws: (any Error).self) { try Wire.decode(Preferences.self, Wire.encode(p)) }
}
@Test func allMutationShapesRoundTrip() throws {
    let s = try fixture()
    let p = PreferencesRequest(schema_version: "1", request_id: "p", data_epoch: s.data_epoch, preferences: s.preferences)
    #expect(try Wire.decode(PreferencesRequest.self, Wire.encode(p)) == p)
    let d = DemoRequest(schema_version: "1", request_id: "d", data_epoch: s.data_epoch, action: "seed", fixture_set: "representative-v1", consent_demo: true)
    #expect(try Wire.decode(DemoRequest.self, Wire.encode(d)) == d)
    let x = DeleteRequest(schema_version: "1", request_id: "x", data_epoch: s.data_epoch, confirm: "delete_all")
    #expect(try Wire.decode(DeleteRequest.self, Wire.encode(x)) == x)
    let r = ProcessRequest(schema_version: "1", request_id: "r", data_epoch: s.data_epoch, synthesis_mode: "disabled")
    #expect(try Wire.decode(ProcessRequest.self, Wire.encode(r)) == r)
}
@Test func emptyHealthQueryDoesNotMintZero() {
    #expect(DailySteps.observation(value: nil, date: "2026-11-13", timezone: "America/New_York", existing: []) == nil)
}
@Test func explicitZeroIsARecordedValue() {
    let o = DailySteps.observation(value: 0, date: "2026-11-13", timezone: "America/New_York", existing: [])
    #expect(o?.activity?.value == 0); #expect(o?.provenance.coverage == "partial")
}
@Test func healthRevisionsKeepIdentity() throws {
    let old = try #require(DailySteps.observation(value: 10, date: "2026-11-13", timezone: "America/New_York", existing: [], newID: "daily-a"))
    #expect(DailySteps.observation(value: 10, date: old.local_date, timezone: old.timezone, existing: [old]) == nil)
    let changed = try #require(DailySteps.observation(value: 12, date: old.local_date, timezone: old.timezone, existing: [old]))
    #expect(changed.id == old.id); #expect(changed.revision == 2)
    #expect(changed.supersedes == EvidenceRef(observation_id: old.id, revision: 1))
}
@Test func timezoneDoesNotRewriteOldRecords() throws {
    let old = try #require(DailySteps.observation(value: 10, date: "2026-11-13", timezone: "America/New_York", existing: [], newID: "old"))
    let next = DailySteps.observation(value: 20, date: old.local_date, timezone: "Europe/London", existing: [old], newID: "new")
    #expect(next?.id == "new"); #expect(next?.supersedes == nil)
}
@Test func permissionRequestNeverClaimsReadGrant() {
    let w = Window(start_date: "2026-11-07", end_date: "2026-11-13", timezone: "America/New_York")
    let empty = DailySteps.update(readable: false, requested: true, window: w, attemptedAt: "2026-11-14T12:00:00Z")
    #expect(empty.read_access == "unknown"); #expect(empty.query_status == "empty")
    let error = DailySteps.update(readable: true, requested: true, window: w, attemptedAt: "2026-11-14T12:00:00Z", error: "protected_data")
    #expect(error.read_access == "unknown"); #expect(error.query_status == "error")
}
@Test func readableOnlyMeansSamplesReadable() {
    let update = DailySteps.update(readable: true, requested: true, window: Window(start_date: "2026-11-07", end_date: "2026-11-13", timezone: "America/New_York"), attemptedAt: "2026-11-14T12:00:00Z")
    #expect(update.read_access == "samples_readable")
}
@Test func newEpochErasesQueueWithoutRetagging() throws {
    let s = try fixture(); let item = try PendingMutation(path: "/feedback", request: feedback())
    var local = LocalSnapshot(state: s, outbox: [item]); var new = s; new.data_epoch = "rotated"
    local.accept(new)
    #expect(local.outbox.isEmpty); #expect(item.epoch == s.data_epoch)
    #expect(try Wire.decode(FeedbackRequest.self, item.body).request_id == "feedback-test")
}
@Test func olderSnapshotCannotReplaceNewerRevision() throws {
    let s = try fixture(); var new = s; new.revision = 99
    var local = LocalSnapshot(state: new); local.accept(s)
    #expect(local.state?.revision == 99)
}
@Test func persistentSnapshotSurvivesRestart() throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let local = LocalSnapshot(state: try fixture(), outbox: [try PendingMutation(path: "/feedback", request: feedback())])
    try storage.save(local); #expect(try storage.load() == local)
    #expect(try storage.directory.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup == true)
}
@Test func corruptStorageDoesNotSilentlyDropOutbox() throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    try storage.save(LocalSnapshot())
    try Data("corrupt".utf8).write(to: storage.directory.appendingPathComponent("snapshot.json"))
    #expect(throws: (any Error).self) { try storage.load() }
}
@Test @MainActor func transientRetryPreservesExactBodyAndID() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let stub = StubHTTP(state: try fixture(), failures: 2)
    let client = try CompanionClient(storage: storage, transport: stub, retryDelay: 0)
    try await client.refresh(); let item = try PendingMutation(path: "/feedback", request: feedback())
    try client.enqueue(item); try await client.synchronize()
    let requests = await stub.mutations()
    #expect(requests.count == 3); #expect(requests.allSatisfy { $0.httpBody == item.body })
    #expect(requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == nil })
    #expect(client.snapshot.outbox.isEmpty); #expect(client.snapshot.state?.revision == 2)
}
@Test @MainActor func boundedFailurePersistsOutbox() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let stub = StubHTTP(state: try fixture(), failures: 20)
    let client = try CompanionClient(storage: storage, transport: stub, retryDelay: 0)
    try await client.refresh(); try client.enqueue(PendingMutation(path: "/feedback", request: feedback()))
    await #expect(throws: (any Error).self) { try await client.synchronize() }
    #expect(await stub.mutations().count == 3); #expect(try storage.load().outbox.count == 1)
}
@Test @MainActor func permanentErrorIsNotBlindlyRetried() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let stub = StubHTTP(state: try fixture(), rejection: "consent_required")
    let client = try CompanionClient(storage: storage, transport: stub, retryDelay: 0)
    try await client.refresh(); try client.enqueue(PendingMutation(path: "/feedback", request: feedback()))
    await #expect(throws: ClientFailure.rejected("consent_required", false)) { try await client.synchronize() }
    #expect(await stub.mutations().count == 1); #expect(client.snapshot.outbox.count == 1)
    let restarted = try CompanionClient(storage: storage, transport: stub, retryDelay: 0)
    await #expect(throws: ClientFailure.rejected("consent_required", false)) { try await restarted.synchronize() }
    #expect(await stub.mutations().count == 1) // persists blocked status: no foreground blind retry
}
@Test @MainActor func remoteEpochChangeNeverUploadsOldQueue() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let s = try fixture(); try storage.save(LocalSnapshot(state: s, outbox: [try PendingMutation(path: "/feedback", request: feedback())]))
    var newer = s; newer.data_epoch = "remote-reset"
    let stub = StubHTTP(state: newer); let client = try CompanionClient(storage: storage, transport: stub)
    try await client.synchronize()
    #expect(await stub.mutations().isEmpty); #expect(client.snapshot.outbox.isEmpty)
}
@Test @MainActor func deleteRotatesAndClearsFollowingMutations() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let stub = StubHTTP(state: try fixture(), rotate: true); let client = try CompanionClient(storage: storage, transport: stub)
    try await client.refresh()
    try client.enqueue(PendingMutation(path: "/data", method: "DELETE", request: DeleteRequest(schema_version: "1", request_id: "delete", data_epoch: "demo-epoch-1", confirm: "delete_all")))
    #expect(client.snapshot.hidesCachedData)
    try client.enqueue(PendingMutation(path: "/feedback", request: feedback()))
    try await client.synchronize()
    #expect(await stub.mutations().count == 1); #expect(client.snapshot.outbox.isEmpty)
    #expect(client.snapshot.state?.observations.isEmpty == true)
}
@Test @MainActor func expiredReceiptDoesNotResend() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let stub = StubHTTP(state: try fixture()); let client = try CompanionClient(storage: storage, transport: stub)
    try await client.refresh()
    try client.enqueue(PendingMutation(path: "/feedback", request: feedback(), now: Date(timeIntervalSince1970: 0)))
    await #expect(throws: ClientFailure.expiredRequest) { try await client.synchronize() }
    #expect(await stub.mutations().isEmpty); #expect(client.snapshot.outbox.isEmpty)
}
@Test func notificationDenialAndSuppressionCancelAll() throws {
    let s = try fixture()
    let plan = NotificationPlan.make(state: s, existing: ["old"], now: Date(), authorized: false, locallyBlocked: false)
    #expect(plan.cancel == ["old"]); #expect(plan.upsert.isEmpty)
}
@Test func notificationReconciliationUsesEpochAndFutureOnly() throws {
    var s = try fixture(); s.preferences.notifications.enabled = true
    s.notification_intents = [NotificationIntent(id: "recorded_steps:2026-11-14", scenario_id: "recorded_steps", card_id: "recorded_steps", scheduled_at: "2026-11-14T18:00:00Z", expires_at: "2026-11-15T00:00:00Z", title: "Your companion", body: "An optional suggestion is ready to review.")]
    let now = ISO8601DateFormatter().date(from: "2026-11-14T12:00:00Z")!
    let plan = NotificationPlan.make(state: s, existing: ["companion:old-epoch:recorded_steps:2026-11-14"], now: now, authorized: true, locallyBlocked: false)
    #expect(plan.cancel.count == 1); #expect(plan.upsert.count == 1)
    let blocked = NotificationPlan.make(state: s, existing: ["existing"], now: now, authorized: true, locallyBlocked: true)
    #expect(blocked.upsert.isEmpty); #expect(blocked.cancel == ["existing"])
    #expect(NotificationPlan.make(state: s, existing: [], now: now.addingTimeInterval(86400), authorized: true, locallyBlocked: false).upsert.isEmpty)
}

actor ReplayHTTP: HTTPTransport {
    let latest: State
    let receipt: State
    init(latest: State, receipt: State) { self.latest = latest; self.receipt = receipt }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        if request.httpMethod == "GET" { return HTTPResult(status: 200, body: try Wire.encode(latest)) }
        return HTTPResult(status: 200, body: try Wire.encode(receipt))
    }
}
@Test @MainActor func idempotentReplayDoesNotRollBackState() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let receipt = try fixture(); var latest = receipt; latest.revision = 20; latest.preferences.no_spending = false
    let client = try CompanionClient(storage: storage, transport: ReplayHTTP(latest: latest, receipt: receipt))
    try await client.refresh(); try client.enqueue(PendingMutation(path: "/feedback", request: feedback()))
    try await client.synchronize()
    #expect(client.snapshot.state == latest); #expect(client.snapshot.outbox.isEmpty)
}
@Test @MainActor func enqueueRejectsStaleEpoch() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let client = try CompanionClient(storage: storage, transport: StubHTTP(state: try fixture()))
    try await client.refresh()
    #expect(throws: ClientFailure.rejected("epoch_mismatch", false)) {
        try client.enqueue(PendingMutation(path: "/feedback", request: feedback(epoch: "old-epoch")))
    }
    #expect(client.snapshot.outbox.isEmpty)
}
@Test @MainActor func endpointCannotUseRemotePlainHTTP() throws {
    #expect(throws: ClientFailure.invalidEndpoint) {
        try CompanionClient(storage: temporaryStorage(), baseURL: URL(string: "http://example.com")!)
    }
}
@Test func mutationRejectsUnknownRouteOrWrongMethod() throws {
    #expect(throws: ClientFailure.rejected("invalid_request", false)) { try PendingMutation(path: "/other", request: feedback()) }
    #expect(throws: ClientFailure.rejected("invalid_request", false)) { try PendingMutation(path: "/feedback", method: "PUT", request: feedback()) }
}
@Test func exportEncodedContractCases() throws {
    let s = try fixture()
    var cases: [[String: Any]] = []
    func add<T: Encodable>(_ name: String, _ value: T) throws {
        cases.append(["definition": name, "payload": try dictionary(value)])
    }
    try add("state", s)
    try add("observation_batch", batchFixture())
    try add("feedback_request", feedback())
    try add("preferences_request", PreferencesRequest(schema_version: "1", request_id: "p", data_epoch: s.data_epoch, preferences: s.preferences))
    try add("process_request", ProcessRequest(schema_version: "1", request_id: "process", data_epoch: s.data_epoch, synthesis_mode: "disabled"))
    try add("demo_request", DemoRequest(schema_version: "1", request_id: "seed", data_epoch: s.data_epoch, action: "seed", fixture_set: "representative-v1", consent_demo: true))
    try add("demo_request", DemoRequest(schema_version: "1", request_id: "reset", data_epoch: s.data_epoch, action: "reset", fixture_set: "representative-v1", consent_demo: true))
    try add("delete_request", DeleteRequest(schema_version: "1", request_id: "delete", data_epoch: s.data_epoch, confirm: "delete_all"))
    let window = Window(start_date: "2026-11-07", end_date: "2026-11-13", timezone: "America/New_York")
    let reading = try #require(DailySteps.observation(value: 123, date: "2026-11-13", timezone: window.timezone, existing: [], newID: "live-steps-test"))
    for (readable, error) in [(false, nil), (true, nil), (false, "protected_data")] as [(Bool, String?)] {
        try add("observation_batch", ObservationBatch(schema_version: "1", request_id: UUID().uuidString, data_epoch: s.data_epoch, observations: readable ? [reading] : [], source_updates: [DailySteps.update(readable: readable, requested: true, window: window, attemptedAt: "2026-11-14T12:00:00Z", error: error)]))
    }
    try add("feedback_request", FeedbackRequest(schema_version: "1", request_id: "event-feedback", data_epoch: s.data_epoch, card_id: "purchase_recurrence", kind: "correctness", value: "partial", scope: "event", event_ref: EvidenceRef(observation_id: "demo-purchase-1", revision: 1), attribution: "shared", note: "Synthetic correction"))
    try add("feedback_request", FeedbackRequest(schema_version: "1", request_id: "usefulness-feedback", data_epoch: s.data_epoch, card_id: "purchase_recurrence", kind: "usefulness", value: "not_useful", scope: "card", event_ref: nil, attribution: nil, note: ""))
    try add("feedback_request", FeedbackRequest(schema_version: "1", request_id: "stop-feedback", data_epoch: s.data_epoch, card_id: "purchase_recurrence", kind: "suppress", value: "stop", scope: "scenario", event_ref: nil, attribution: nil, note: ""))
    let fe = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let evidence = ProcessInfo.processInfo.environment["COMPANION_EVIDENCE_DIR"].map { URL(fileURLWithPath: $0) } ?? fe.appendingPathComponent("evidence")
    try FileManager.default.createDirectory(at: evidence, withIntermediateDirectories: true)
    try JSONSerialization.data(withJSONObject: cases, options: [.prettyPrinted, .sortedKeys]).write(to: evidence.appendingPathComponent("encoded-contracts.json"), options: .atomic)
    #expect(cases.count == 14)
}

@Test func readOnlyPreviewRejectsEveryMutationWithoutReachingTransport() async throws {
    let underlying = StubHTTP(state: try fixture())
    let transport = ReadOnlyTransport(base: underlying)
    for path in ["/connections/plaid", "/preferences", "/data", "/feedback", "/process", "/observations", "/demo"] {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:8000" + path)!)
        request.httpMethod = path == "/data" ? "DELETE" : "POST"
        await #expect(throws: ClientFailure.rejected("read_only_preview", false)) { try await transport.send(request) }
    }
    #expect(await underlying.requests.isEmpty)
}
@Test func readOnlyPreviewCanReadState() async throws {
    let underlying = StubHTTP(state: try fixture())
    let transport = ReadOnlyTransport(base: underlying)
    let result = try await transport.send(URLRequest(url: URL(string: "http://127.0.0.1:8000/state")!))
    #expect(result.status == 200); #expect(await underlying.requests.count == 1)
}


@Test func debugLoopbackPortOverrideIsExplicitAndValidated() throws {
    #expect(try LoopbackEndpoint.port(arguments: [], environment: [:], allowOverride: true) == 8000)
    #expect(try LoopbackEndpoint.port(arguments: ["--service-port", "8768"], environment: [:], allowOverride: true) == 8768)
    #expect(try LoopbackEndpoint.port(arguments: [], environment: ["LOCAL_COMPANION_TEST_PORT": "8768"], allowOverride: true) == 8768)
    #expect(try LoopbackEndpoint.port(arguments: ["--service-port", "8768"], environment: [:], allowOverride: false) == 8000)
    for bad in ["0", "65536", "-1", "https://example.com", "127.0.0.1:8768", "8768/path", ""] {
        #expect(throws: ClientFailure.invalidEndpoint) { try LoopbackEndpoint.port(arguments: ["--service-port", bad], environment: [:], allowOverride: true) }
    }
    #expect(throws: ClientFailure.invalidEndpoint) { try LoopbackEndpoint.port(arguments: ["--service-port"], environment: [:], allowOverride: true) }
}
@Test @MainActor func clientExplicitOriginAllowsLocalPortButRejectsMismatchedPortAssertion() throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let url = try LoopbackEndpoint.url(port: 8768)
    _ = try CompanionClient(storage: storage, baseURL: url)
    #expect(throws: ClientFailure.invalidEndpoint) { try CompanionClient(storage: storage, baseURL: url, allowedPort: 8769) }
    _ = try CompanionClient(storage: storage, baseURL: url, allowedPort: 8768)
    #expect(throws: ClientFailure.invalidEndpoint) { try CompanionClient(storage: storage, baseURL: URL(string: "http://example.com:8768")!, allowedPort: 8768) }
}
