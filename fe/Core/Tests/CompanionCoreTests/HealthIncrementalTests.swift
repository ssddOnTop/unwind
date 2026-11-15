import Foundation
import Testing
@testable import CompanionCore

@MainActor private final class IncrementalHTTP: HTTPTransport {
    var calls: [URLRequest] = []
    var fail = false
    var wrongReceipt = false
    var healthAllowed = true
    func send(_ request: URLRequest) async throws -> HTTPResult {
        calls.append(request)
        if request.url?.path == "/state" || request.url?.path == "/preferences" {
            if request.url?.path == "/preferences" { healthAllowed = try Wire.decode(PreferencesRequest.self, #require(request.httpBody)).preferences.enabled_source_ids.contains("healthkit") }
            var state = try fixture()
            if let i = state.sources.firstIndex(where: { $0.id == "healthkit" }) { state.sources[i].enabled = healthAllowed; state.sources[i].consent = healthAllowed ? "granted" : "revoked" }
            return HTTPResult(status: 200, body: try Wire.encode(state))
        }
        if fail { throw URLError(.networkConnectionLost) }
        let body = try Wire.decode(HealthIncrementalRequest.self, #require(request.httpBody))
        let value = HealthIncrementalReceipt(schema_version: "1", request_id: wrongReceipt ? UUID().uuidString : body.request_id, data_epoch: body.data_epoch, stream_id: body.stream_id, sequence: body.sequence, hk_type: body.hk_type, upserted: body.samples.count, deleted: body.deleted_uuids.count, excluded: 0, expired: 0, superseded: 0, unknown_deletions: 0, bootstrapped: !body.has_more)
        return HTTPResult(status: 200, body: try Wire.encode(value))
    }
}
private func deltaSample() -> IncrementalSample {
    IncrementalSample(hk_uuid: "00000000-0000-4000-8000-000000000001", hk_type: "heartRate", start_at: "2026-11-13T10:00:00Z", end_at: "2026-11-13T10:01:00Z", timezone: "UTC", value: 70, unit: "bpm", category: nil, workout_activity: nil, duration_seconds: nil, energy_kcal: nil, distance_m: nil, source_name: "Synthetic test", source_bundle: "test.health", source_revision: "1", source_stream: nil, synthetic: false, dataset_id: nil, recipe: nil, metadata_timezone: nil, excluded_reason: nil)
}

@Test @MainActor func healthAtomicAdmissionSurvivesRestartAndTerminalReplayWithoutStateReplacement() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = IncrementalHTTP(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh(); let epoch = try #require(client.snapshot.state?.data_epoch)
    let start = Date(timeIntervalSince1970: 1_700_000_000), anchor = Data([1,2,3])
    try client.saveHealthCheckpoint(type: "heartRate", checkpoint: HealthIncrementalCheckpoint(start: start, anchor: nil, terminalPending: false), epoch: epoch)
    try client.admitHealthDelta(type: "heartRate", epoch: epoch, samples: [deltaSample()], deletedUUIDs: [], anchor: anchor, hasMore: true)
    let first = try #require(client.snapshot.outbox.first)
    #expect(client.healthCheckpoint(type: "heartRate")?.anchor == anchor)
    #expect(client.healthCheckpoint(type: "heartRate")?.terminalPending == true)
    // Retry after a failed separate checkpoint save is byte-identical, not another sequence.
    try client.admitHealthDelta(type: "heartRate", epoch: epoch, samples: [deltaSample()], deletedUUIDs: [], anchor: anchor, hasMore: true)
    #expect(client.snapshot.outbox == [first])
    let restarted = try CompanionClient(storage: storage, transport: http)
    http.fail = true
    await #expect(throws: (any Error).self) { try await restarted.drainHealthDeltas(using: HealthOnlyTransport(base: http)) }
    #expect(restarted.snapshot.outbox == [first])
    http.fail = false; http.wrongReceipt = true
    await #expect(throws: (any Error).self) { try await restarted.drainHealthDeltas(using: HealthOnlyTransport(base: http)) }
    #expect(restarted.snapshot.outbox == [first])
    http.wrongReceipt = false
    let state = restarted.snapshot.state
    try await restarted.drainHealthDeltas(using: HealthOnlyTransport(base: http))
    #expect(restarted.snapshot.outbox.isEmpty && restarted.snapshot.state == state)
    try restarted.admitHealthDelta(type: "heartRate", epoch: epoch, samples: [], deletedUUIDs: [], anchor: anchor, hasMore: false)
    let terminal = try Wire.decode(HealthIncrementalRequest.self, #require(restarted.snapshot.outbox.first).body)
    #expect(terminal.sequence == 1 && terminal.samples.isEmpty && !terminal.has_more)
    #expect(terminal.stream_id == (try Wire.decode(HealthIncrementalRequest.self,first.body)).stream_id)
    try await restarted.drainHealthDeltas(using: HealthOnlyTransport(base: http))
    #expect(restarted.healthCheckpoint(type: "heartRate")?.terminalPending == false)
}

@Test @MainActor func healthJournalWriteFailureCannotAdvanceAnchorOrLoseOutboxAndDiscardRetainsIt() async throws {
    let storage = temporaryStorage(); let moved = storage.directory.appendingPathExtension("saved")
    defer { try? FileManager.default.removeItem(at: storage.directory); try? FileManager.default.removeItem(at: moved) }
    let http = IncrementalHTTP(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh(); let epoch = try #require(client.snapshot.state?.data_epoch)
    let initial = HealthIncrementalCheckpoint(start: Date(timeIntervalSince1970: 1_700_000_000), anchor: nil, terminalPending: false)
    try client.saveHealthCheckpoint(type: "heartRate", checkpoint: initial, epoch: epoch)
    try FileManager.default.moveItem(at: storage.directory, to: moved)
    try Data("not-a-directory".utf8).write(to: storage.directory)
    #expect(throws: (any Error).self) { try client.admitHealthDelta(type: "heartRate", epoch: epoch, samples: [deltaSample()], deletedUUIDs: [], anchor: Data([8]), hasMore: true) }
    #expect(client.healthCheckpoint(type: "heartRate") == initial && client.snapshot.outbox.isEmpty)
    try FileManager.default.removeItem(at: storage.directory); try FileManager.default.moveItem(at: moved, to: storage.directory)
    #expect(try storage.load().healthStreams == client.snapshot.healthStreams)
    try client.admitHealthDelta(type: "heartRate", epoch: epoch, samples: [deltaSample()], deletedUUIDs: [], anchor: Data([8]), hasMore: true)
    let first = client.snapshot.outbox
    try client.discardPending()
    #expect(client.snapshot.outbox == first && client.healthCheckpoint(type: "heartRate")?.anchor == Data([8]))
}

@Test @MainActor func healthOldOutboxStillReplaysAndSeparateBackgroundAuthorityCannotUnlockForeground() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = IncrementalHTTP(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh(); let epoch = try #require(client.snapshot.state?.data_epoch)
    let body = HealthIncrementalRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: epoch, hk_type: "heartRate", stream_id: UUID().uuidString, sequence: 0, has_more: false, samples: [deltaSample()], deleted_uuids: [])
    try client.enqueue(PendingMutation(path: "/health/v1/incremental", request: body, now: Date().addingTimeInterval(-400*86400)))
    try await client.synchronize(); #expect(client.snapshot.outbox.isEmpty)
    let background = HealthOnlyTransport(base: http)
    let before = http.calls.count
    for path in ["/state", "/process", "/patterns/v1/analyze", "/banking/v1/actions", "/workflows/v1/enqueue"] {
        var req = URLRequest(url: URL(string: "http://localhost:8769" + path)!); req.httpMethod = "POST"
        await #expect(throws: (any Error).self) { try await background.send(req) }
    }
    #expect(http.calls.count == before)
    client.invalidateSession()
    await #expect(throws: (any Error).self) { try await client.drainHealthDeltas(using: background) }
    #expect(http.calls.count == before)
}

@Test func healthReceiptRequiresExpiredCountAndRejectsExtraData() throws {
    let value = HealthIncrementalReceipt(schema_version: "1", request_id: UUID().uuidString, data_epoch: "test", stream_id: UUID().uuidString, sequence: 0, hk_type: "heartRate", upserted: 1, deleted: 0, excluded: 0, expired: 3, superseded: 0, unknown_deletions: 0, bootstrapped: true)
    var object = try dictionary(value); object.removeValue(forKey: "expired")
    #expect(throws: (any Error).self) { try Wire.decode(HealthIncrementalReceipt.self, JSONSerialization.data(withJSONObject: object)) }
    object = try dictionary(value); object["account_id"] = "not-accepted"
    #expect(throws: (any Error).self) { try Wire.decode(HealthIncrementalReceipt.self, JSONSerialization.data(withJSONObject: object)) }
}

@MainActor private final class HealthTokens: SessionTokenProvider {
    var currentIdentity: AuthIdentity? = AuthIdentity(issuer: "https://health-test.clerk.accounts.dev", subject: "unit-user", sessionID: "unit-session")
    func token(for identity: AuthIdentity, forceRefresh: Bool) async throws -> String? { "unit-token" }
}
@MainActor private final class HealthAdmissionHTTP: HTTPTransport {
    var paths: [String] = []
    func send(_ request: URLRequest) async throws -> HTTPResult {
        paths.append(request.url!.path)
        return HTTPResult(status: 200, body: Data(#"{"schema_version":"1","issuer":"https://health-test.clerk.accounts.dev","subject":"unit-user"}"#.utf8), contentType: "application/json")
    }
}
// Explicit barriers make overlap independent of timing, network, or HealthKit.
@MainActor private final class HealthAdmissionGate: HTTPTransport {
    let deltas = IncrementalHTTP()
    var handshakes = 0
    var heldPath = "/auth/v1/session"
    var entered: CheckedContinuation<Void, Never>?
    var release: CheckedContinuation<Void, Never>?
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let path = request.url!.path
        if path == "/auth/v1/session" { handshakes += 1 }
        if path == heldPath {
            await withCheckedContinuation { continuation in
                release = continuation
                entered?.resume(); entered = nil
            }
        }
        if path == "/auth/v1/session" {
            return HTTPResult(status: 200, body: Data(#"{"schema_version":"1","issuer":"https://health-test.clerk.accounts.dev","subject":"unit-user"}"#.utf8), contentType: "application/json")
        }
        return try await deltas.send(request)
    }
    func resume() { heldPath = ""; release?.resume(); release = nil }
}

@Test @MainActor func healthOverlappingAdmissionsShareHandshakeAndRejectLateLogoutCompletion() async throws {
    for logout in [false, true] {
        let provider = HealthTokens(), network = HealthAdmissionGate()
        let health = try HealthBackgroundLease(identity: #require(provider.currentIdentity), provider: provider, base: network, origin: URL(string: "http://127.0.0.1:8769")!)
        var first: Task<Void, Error>!
        await withCheckedContinuation { entered in
            network.entered = entered
            first = Task { try await health.admit() }
        }
        var second: Task<Void, Error>!
        await withCheckedContinuation { started in
            second = Task { @MainActor in
                started.resume()
                try await health.admit()
            }
        }
        #expect(network.handshakes == 1)
        if logout { health.stop() }
        network.resume() // Deliberately ignores cancellation, like a late HTTP response.
        if logout {
            await #expect(throws: (any Error).self) { try await first.value }
            await #expect(throws: (any Error).self) { try await second.value }
            #expect(!health.transport.isAdmitted)
            await #expect(throws: (any Error).self) { try await health.admit() }
        } else {
            try await first.value; try await second.value
            try health.validate()
            // An explicit invalidation still requires a fresh handshake.
            health.transport.invalidateAdmission()
            try await health.admit()
            #expect(network.handshakes == 2)
        }
    }
}

@Test @MainActor func healthOverlappingTypeDrainReusesAdmissionAndFencesLateAck() async throws {
    for logout in [false, true] {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let provider = HealthTokens(), network = HealthAdmissionGate()
        network.heldPath = ""
        let health = try HealthBackgroundLease(identity: #require(provider.currentIdentity), provider: provider, base: network, origin: URL(string: "http://127.0.0.1:8769")!)
        let client = try CompanionClient(storage: storage, transport: network.deltas, baseURL: URL(string: "http://127.0.0.1:8769")!)
        try await client.refresh()
        client.setHealthSessionValidator { try health.validate() }
        let epoch = try #require(client.snapshot.state?.data_epoch)
        let background = HealthOnlyTransport(base: health.transport)
        try await health.admit()
        for type in ["heartRate", "stepCount"] {
            try client.saveHealthCheckpoint(type: type, checkpoint: HealthIncrementalCheckpoint(start: Date(timeIntervalSince1970: 1_700_000_000), anchor: nil, terminalPending: false), epoch: epoch)
        }
        try client.admitHealthDelta(type: "heartRate", epoch: epoch, samples: [], deletedUUIDs: [], anchor: Data([1]), hasMore: false)
        network.heldPath = "/health/v1/incremental"
        var first: Task<Void, Error>!
        await withCheckedContinuation { entered in
            network.entered = entered
            first = Task { try await client.drainHealthDeltas(using: background) }
        }
        // The sibling type enters while the first type's acknowledgement is held.
        try await health.admit()
        try client.admitHealthDelta(type: "stepCount", epoch: epoch, samples: [], deletedUUIDs: [], anchor: Data([2]), hasMore: false)
        await #expect(throws: ClientFailure.busy) { try await client.drainHealthDeltas(using: background) }
        let pending = client.snapshot.outbox
        #expect(network.handshakes == 1)
        if logout { health.stop() }
        network.resume()
        if logout {
            await #expect(throws: (any Error).self) { try await first.value }
            #expect(client.snapshot.outbox == pending)
            #expect(try storage.load().outbox == pending)
        } else {
            try await first.value
            #expect(client.snapshot.outbox.isEmpty)
            #expect(try storage.load().outbox.isEmpty)
            #expect(network.deltas.calls.filter { $0.url?.path == "/health/v1/incremental" }.count == 2)
        }
    }
}

@Test @MainActor func healthBackgroundLeaseSurvivesUISuspensionButNotIdentityChangeOrLogout() async throws {
    let provider = HealthTokens(), network = HealthAdmissionHTTP(), foreground = AuthSessionFence()
    let identity = try #require(provider.currentIdentity), origin = URL(string: "http://127.0.0.1:8769")!
    let uiLease = foreground.activate(identity)
    let health = try HealthBackgroundLease(identity: identity, provider: provider, base: network, origin: origin)
    try await health.admit(); try health.validate()
    foreground.suspend()
    #expect(throws: AuthenticationFailure.locked) { try foreground.validate(uiLease) }
    try await health.admit(); try health.validate()
    #expect(foreground.identity == nil, "Health auth cannot reactivate the UI fence")
    let count = network.paths.count
    provider.currentIdentity = AuthIdentity(issuer: identity.issuer, subject: "different-user", sessionID: "different-session")
    await #expect(throws: (any Error).self) { try await health.admit() }
    #expect(network.paths.count == count)
    provider.currentIdentity = identity; health.stop()
    #expect(throws: (any Error).self) { try health.validate() }
    await #expect(throws: (any Error).self) { try await health.admit() }
    #expect(network.paths.count == count)
}


@Test @MainActor func healthConsentRevocationPrecedesQueuedDeltasWithoutDiscardingTheirAnchorObligation() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = IncrementalHTTP(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh(); let state = try #require(client.snapshot.state)
    try client.saveHealthCheckpoint(type: "heartRate", checkpoint: HealthIncrementalCheckpoint(start: Date(timeIntervalSince1970: 1_700_000_000), anchor: nil, terminalPending: false), epoch: state.data_epoch)
    try client.admitHealthDelta(type: "heartRate", epoch: state.data_epoch, samples: [deltaSample()], deletedUUIDs: [], anchor: Data([9]), hasMore: true)
    let pending = try #require(client.snapshot.outbox.first)
    var prefs = state.preferences; prefs.enabled_source_ids.removeAll { $0 == "healthkit" }
    try client.enqueue(PendingMutation(path: "/preferences", method: "PUT", request: PreferencesRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, preferences: prefs)))
    #expect(client.snapshot.outbox.first?.path == "/preferences")
    try await client.synchronize()
    #expect(!http.healthAllowed && client.snapshot.outbox == [pending])
    #expect(!client.snapshot.hasGeneralPendingChanges)
    #expect(http.calls.allSatisfy { $0.url?.path != "/health/v1/incremental" })
    try client.discardPending(); #expect(client.snapshot.outbox == [pending])
    #expect(client.healthCheckpoint(type: "heartRate")?.anchor == Data([9]))
}
