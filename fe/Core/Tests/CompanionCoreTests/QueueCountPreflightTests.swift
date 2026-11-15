import Foundation
import Testing
@testable import CompanionCore

private let preflightIdentity = AuthIdentity(issuer: "https://preflight.invalid", subject: "synthetic", sessionID: "synthetic-session")
private func preflightTree(_ root: URL) throws -> [String: Data] {
    var values: [String: Data] = [:]
    let paths = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isRegularFileKey])!
    for case let url as URL in paths where try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true {
        values[url.path] = try Data(contentsOf: url)
        values[url.path + ".mtime"] = Data(String(describing: try FileManager.default.attributesOfItem(atPath: url.path)[.modificationDate]).utf8)
    }
    return values
}
@MainActor private final class PreflightTokens: SessionTokenProvider {
    var currentIdentity: AuthIdentity? = preflightIdentity
    func token(for identity: AuthIdentity, forceRefresh: Bool) async throws -> String? { "synthetic-token" }
}
@MainActor private final class PreflightHTTP: HTTPTransport {
    var requests: [URLRequest] = []
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        #expect(request.httpMethod == "GET" && request.url?.path == "/auth/v1/session")
        return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: ["schema_version": "1", "issuer": preflightIdentity.issuer, "subject": preflightIdentity.subject]), contentType: "application/json")
    }
}

@Test @MainActor func queuePreflightPendingQueuesRemainByteIdenticalAndOnlyAdmissionIsSent() async throws {
    let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
    let cache = AccountCache(root: root)
    let storage = try cache.activate(preflightIdentity)
    let state = try fixture()
    let health = HealthIncrementalRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, hk_type: "heartRate", stream_id: UUID().uuidString, sequence: 0, has_more: false, samples: [], deleted_uuids: [])
    let snapshot = LocalSnapshot(state: state, outbox: [try PendingMutation(path: "/feedback", request: feedback(epoch: state.data_epoch)), try PendingMutation(path: "/health/v1/incremental", request: health)])
    try storage.save(snapshot)
    let suite = "preflight-test-" + UUID().uuidString
    let defaults = UserDefaults(suiteName: suite)!; defer { defaults.removePersistentDomain(forName: suite) }
    let key = "report-notification-admission.v1." + preflightIdentity.accountNamespace
    let notification = try JSONSerialization.data(withJSONObject: ["requestID":"synthetic", "epoch":state.data_epoch, "binding":"synthetic", "pendingRevoke":true])
    defaults.set(notification, forKey: key)
    let before = try preflightTree(root)
    let http = PreflightHTTP(), tokens = PreflightTokens(), fence = AuthSessionFence()
    let lease = fence.activate(preflightIdentity)
    let transport = try AuthenticatedTransport(base: http, provider: tokens, fence: fence, lease: lease, origin: URL(string: "http://127.0.0.1:9876")!)
    var emitted: [String] = []
    let preflight = QueueCountPreflight { emitted.append($0) }
    await preflight.run(admit: { try await transport.admit() }, read: {
        try transport.validate()
        let loaded = try cache.preflightSnapshot(preflightIdentity)
        let count = try ReportNotificationController.preflightPendingRevocations(accountNamespace: preflightIdentity.accountNamespace, epoch: state.data_epoch, defaults: defaults)
        return .init(snapshot: loaded, notificationRevocations: count)
    })
    #expect(emitted == ["queue-preflight-v1 status=available general=1 health=1 notification_revocations=1"])
    #expect(http.requests.count == 1)
    #expect(try preflightTree(root) == before)
    #expect(defaults.data(forKey: key) == notification)
    // Reentry/foreground attempts cannot read again, send, or change the completed result.
    preflight.stop()
    await preflight.run(admit: { Issue.record("Unexpected second admission") }, read: { Issue.record("Unexpected reread"); throw AuthenticationFailure.unavailable })
    #expect(emitted.count == 1 && http.requests.count == 1)
}

@Test @MainActor func queuePreflightMissingCorruptOrMismatchedCacheIsUnavailableWithoutWrites() async throws {
    for mode in ["missing", "corrupt", "unreadable", "identity", "deployment"] {
        let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
        let cache = AccountCache(root: root)
        let storage = try cache.activate(preflightIdentity)
        let file = storage.directory.appendingPathComponent("snapshot.json")
        if mode != "missing" { try storage.save(LocalSnapshot(state: fixture())) }
        if mode == "corrupt" { try Data("PRIVATE_SENTINEL".utf8).write(to: file) }
        if mode == "unreadable" { try FileManager.default.removeItem(at: file); try FileManager.default.createDirectory(at: file, withIntermediateDirectories: false) }
        let reader = mode == "deployment" ? AccountCache(root: root, deployment: "different") : cache
        let identity = mode == "identity" ? AuthIdentity(issuer: preflightIdentity.issuer, subject: "different", sessionID: "synthetic") : preflightIdentity
        let before = try preflightTree(root)
        var emitted: [String] = []
        let preflight = QueueCountPreflight { emitted.append($0) }
        await preflight.run(admit: {}, read: { .init(snapshot: try reader.preflightSnapshot(identity), notificationRevocations: 0) })
        #expect(emitted == ["queue-preflight-v1 status=unavailable"])
        #expect(try preflightTree(root) == before)
    }
}

@Test @MainActor func queuePreflightMalformedNotificationMetadataIsNotZero() throws {
    let suite = "preflight-test-" + UUID().uuidString
    let defaults = UserDefaults(suiteName: suite)!; defer { defaults.removePersistentDomain(forName: suite) }
    let key = "report-notification-admission.v1." + preflightIdentity.accountNamespace
    #expect(try ReportNotificationController.preflightPendingRevocations(accountNamespace: preflightIdentity.accountNamespace, epoch: "synthetic", defaults: defaults) == 0)
    for bad: Any in ["PRIVATE_SENTINEL", Data("PRIVATE_SENTINEL".utf8), try JSONSerialization.data(withJSONObject: ["requestID":"synthetic", "epoch":"different", "binding":"synthetic", "pendingRevoke":true])] {
        defaults.set(bad, forKey: key)
        #expect(throws: (any Error).self) { try ReportNotificationController.preflightPendingRevocations(accountNamespace: preflightIdentity.accountNamespace, epoch: "synthetic", defaults: defaults) }
        #expect(defaults.object(forKey: key) != nil)
    }
}

@Test @MainActor func queuePreflightAdmissionFailureNeverReadsCache() async {
    var emitted: [String] = []
    let preflight = QueueCountPreflight { emitted.append($0) }
    await preflight.run(admit: { throw AuthenticationFailure.unauthorized }, read: { Issue.record("Read before admission"); throw AuthenticationFailure.unavailable })
    #expect(emitted == ["queue-preflight-v1 status=unavailable"])
}

@Test @MainActor func queuePreflightBackgroundDuringAdmissionBlocksReadAndReentry() async {
    var emitted: [String] = []
    let preflight = QueueCountPreflight { emitted.append($0) }
    await preflight.run(admit: {
        // A lifecycle event can arrive while ordinary SDK admission is suspended.
        preflight.stop()
        await preflight.run(admit: { Issue.record("Reentrant admission") }, read: { Issue.record("Reentrant read"); throw AuthenticationFailure.unavailable })
    }, read: { Issue.record("Read after background"); throw AuthenticationFailure.unavailable })
    await preflight.run(admit: { Issue.record("Foreground admission") }, read: { Issue.record("Foreground read"); throw AuthenticationFailure.unavailable })
    #expect(emitted == ["queue-preflight-v1 status=unavailable"])
}

@Test @MainActor func queuePreflightCoordinatorGuardsSideEffectsAndNormalModeRemainsOptIn() throws {
    let frontend = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let source = try String(contentsOf: frontend.appendingPathComponent("App/ClerkSessionCoordinator.swift"), encoding: .utf8)
    for signature in ["private func clearLocally(persistLock: Bool)", "private func cancelLocalReminders()", "private func prepare(_ identity: AuthIdentity)", "func resumeFromBackground()", "func signOut()", "func retryService()", "func retrySDK() async"] {
        #expect(source.contains(signature + " {\n        guard !isCountPreflight else { return }"))
    }
    #expect(source.contains("if let countPreflight { countPreflight.stop(); work?.cancel(); bearer?.cancel(); return }"))
    #expect(source.contains("guard !isCountPreflight else { return false }"))
    let start = try #require(source.range(of: "private func runCountPreflight()"))
    let end = try #require(source.range(of: "private func revokeCapturedNotificationSession"))
    let body = String(source[start.lowerBound..<end.lowerBound])
    for forbidden in ["cache.activate", "clearActive", "AppModel(", "HealthBackgroundLease(", ".refresh(", "defaults.set", ".save(", "synchronize("] { #expect(!body.contains(forbidden)) }
    #expect(body.contains("try await transport.admit()"))
    #expect(body.contains("try bearer.validate()"))
    #expect(body.contains("cache.preflightSnapshot(identity)"))
    #expect(!QueueCountPreflight.requested) // macOS tests cannot enable simulator-only launch mode.
}
