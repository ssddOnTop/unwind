import Foundation
import Testing
@testable import CompanionCore

private let issuer = "https://unit-test.clerk.accounts.dev"
private let origin = URL(string: "http://127.0.0.1:9876")!
private func identity(_ user: String = "user_A", session: String = "session_A") -> AuthIdentity {
    AuthIdentity(issuer: issuer, subject: user, sessionID: session)
}
private func admission(_ user: String = "user_A", issuer value: String = issuer) throws -> HTTPResult {
    HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: ["schema_version": "1", "issuer": value, "subject": user]), contentType: "application/json; charset=utf-8")
}
private func request(_ path: String = "/feedback", body: Data? = nil) -> URLRequest {
    var value = URLRequest(url: origin.appendingPathComponent(String(path.dropFirst())))
    value.httpMethod = body == nil ? "GET" : "POST"; value.httpBody = body
    value.setValue("application/json", forHTTPHeaderField: "Content-Type")
    return value
}
@MainActor private final class Tokens: SessionTokenProvider {
    var currentIdentity: AuthIdentity? = identity()
    var forced = 0
    var normal = 0
    var hook: ((Bool) async throws -> String?)?
    func token(for identity: AuthIdentity, forceRefresh: Bool) async throws -> String? {
        if forceRefresh { forced += 1 } else { normal += 1 }
        if let hook { return try await hook(forceRefresh) }
        return forced > 0 ? "unit-refreshed-token" : "unit-initial-token"
    }
}
@MainActor private final class Network: HTTPTransport {
    var requests: [URLRequest] = []
    var hook: ((URLRequest) async throws -> HTTPResult)?
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        if let hook { return try await hook(request) }
        return request.url?.path == "/auth/v1/session" ? try admission() : HTTPResult(status: 200, body: Data("{}".utf8))
    }
}
@MainActor private final class Gate {
    var entered = false
    var continuation: CheckedContinuation<Void, Never>?
    func pause() async { entered = true; await withCheckedContinuation { continuation = $0 } }
    func wait() async throws {
        for _ in 0..<20_000 { if entered { return }; await Task.yield() }
        throw ClientFailure.invalidResponse
    }
    func release() { continuation?.resume(); continuation = nil }
}
@MainActor private func transport(_ tokens: Tokens, _ network: Network, _ fence: AuthSessionFence) throws -> AuthenticatedTransport {
    try AuthenticatedTransport(base: network, provider: tokens, fence: fence, lease: fence.activate(tokens.currentIdentity!), origin: origin)
}

@Test @MainActor func admissionRequiredBeforeDataOrOpeningClientCache() async throws {
    let network = Network(), tokens = Tokens(), fence = AuthSessionFence()
    let transport = try transport(tokens, network, fence)
    await #expect(throws: AuthenticationFailure.notAdmitted) { try await transport.send(request("/state")) }
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    #expect(throws: AuthenticationFailure.notAdmitted) { try CompanionClient(storage: storage, transport: transport, sessionValidator: { try transport.validate() }) }
    #expect(network.requests.isEmpty)
    #expect(!FileManager.default.fileExists(atPath: storage.directory.path))
    try await transport.admit()
    _ = try await transport.send(request("/state"))
    #expect(network.requests.map { $0.url!.path } == ["/auth/v1/session", "/state"])
    #expect(network.requests.allSatisfy { $0.cachePolicy == .reloadIgnoringLocalCacheData && $0.value(forHTTPHeaderField: "Cache-Control") == "no-store" })
}
@Test @MainActor func admissionRejectsLegacyRedirect304AndWrongContentTypeWithoutDataProbe() async throws {
    for result in [HTTPResult(status: 404, body: Data()), HTTPResult(status: 405, body: Data()), HTTPResult(status: 302, body: Data()), HTTPResult(status: 304, body: Data()), HTTPResult(status: 200, body: try admission().body, contentType: "text/html")] {
        let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
        network.hook = { _ in result }; let transport = try transport(tokens, network, fence)
        await #expect(throws: AuthenticationFailure.profileMismatch) { try await transport.admit() }
        #expect(!transport.isAdmitted); #expect(network.requests.count == 1)
        await #expect(throws: AuthenticationFailure.notAdmitted) { try await transport.send(request("/state")) }
    }
}
@Test func admissionStrictStringsExactFieldsAndDuplicateKeys() throws {
    let valid = "{\"schema_version\":\"1\",\"issuer\":\"https://unit.test\",\"subject\":\"A\"}"
    #expect(try AuthAdmission.decode(Data(valid.utf8)).subject == "A")
    for invalid in [valid.dropLast() + ",\"subject\":\"B\"}", valid.dropLast() + ",\"sub\\u006aect\":\"B\"}", valid.dropLast() + ",\"extra\":\"bad\"}", valid.replacingOccurrences(of: "\"1\"", with: "1"), valid.replacingOccurrences(of: "\"1\"", with: "\"2\""), valid.replacingOccurrences(of: "\"A\"", with: "null"), valid.replacingOccurrences(of: "\"A\"", with: "true"), valid + "{}", "{}"] {
        #expect(throws: AuthenticationFailure.profileMismatch) { try AuthAdmission.decode(Data(invalid.utf8)) }
    }
}
@Test @MainActor func admissionRejectsWrongUserAndExactIssuerMismatch() async throws {
    for result in [try admission("user_B"), try admission(issuer: issuer + "/")] {
        let network = Network(), tokens = Tokens(), fence = AuthSessionFence()
        network.hook = { _ in result }; let transport = try transport(tokens, network, fence)
        await #expect(throws: AuthenticationFailure.profileMismatch) { try await transport.admit() }
        #expect(!transport.isAdmitted); #expect(network.requests.allSatisfy { $0.url?.path == "/auth/v1/session" })
    }
}
@Test @MainActor func admission401RefreshesExactlyOnceForCapturedSession() async throws {
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    network.hook = { _ in network.requests.count == 1 ? HTTPResult(status: 401, body: Data()) : try admission() }
    let transport = try transport(tokens, network, fence); try await transport.admit()
    #expect(tokens.forced == 1); #expect(network.requests.count == 2); #expect(transport.isAdmitted)
}
@Test @MainActor func admissionSecond401LocksWithoutDomainCalls() async throws {
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    network.hook = { _ in HTTPResult(status: 401, body: Data()) }
    let transport = try transport(tokens, network, fence)
    await #expect(throws: AuthenticationFailure.unauthorized) { try await transport.admit() }
    #expect(tokens.forced == 1); #expect(network.requests.count == 2); #expect(fence.identity == nil)
}
@Test @MainActor func data401ReadmitsThenRetriesIdenticalMutationBytes() async throws {
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    var mutations = 0
    network.hook = { r in
        if r.url?.path == "/auth/v1/session" { return try admission() }
        mutations += 1; return HTTPResult(status: mutations == 1 ? 401 : 200, body: Data("{}".utf8))
    }
    let transport = try transport(tokens, network, fence); try await transport.admit()
    let bytes = try Wire.encode(feedback())
    _ = try await transport.send(request(body: bytes))
    #expect(network.requests.map { $0.url!.path } == ["/auth/v1/session", "/feedback", "/auth/v1/session", "/feedback"])
    let sent = network.requests.filter { $0.url?.path == "/feedback" }
    #expect(sent[0].httpBody == bytes); #expect(sent[1].httpBody == bytes)
    #expect(sent[0].httpMethod == sent[1].httpMethod); #expect(sent[0].url == sent[1].url)
    #expect(sent[0].value(forHTTPHeaderField: "Authorization") == "Bearer unit-initial-token")
    #expect(sent[1].value(forHTTPHeaderField: "Authorization") == "Bearer unit-refreshed-token")
    #expect(tokens.forced == 1)
}
@Test @MainActor func dataAndReadmissionShareOneRefreshBudget() async throws {
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    network.hook = { _ in network.requests.count == 1 ? try admission() : HTTPResult(status: 401, body: Data()) }
    let transport = try transport(tokens, network, fence); try await transport.admit()
    await #expect(throws: AuthenticationFailure.unauthorized) { try await transport.send(request(body: Wire.encode(feedback()))) }
    #expect(tokens.forced == 1); #expect(network.requests.count == 3)
    #expect(fence.identity == nil)
}
@Test @MainActor func secondData401LocksWithoutThirdReplay() async throws {
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    network.hook = { r in r.url?.path == "/auth/v1/session" ? try admission() : HTTPResult(status: 401, body: Data()) }
    let transport = try transport(tokens, network, fence); try await transport.admit()
    await #expect(throws: AuthenticationFailure.unauthorized) { try await transport.send(request(body: Wire.encode(feedback()))) }
    #expect(tokens.forced == 1); #expect(network.requests.filter { $0.url?.path == "/feedback" }.count == 2)
    #expect(fence.identity == nil)
}
@Test @MainActor func pendingAndUnavailableNeverRefreshLoop() async throws {
    for status in [403, 503] {
        let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
        network.hook = { _ in HTTPResult(status: status, body: Data()) }
        let transport = try transport(tokens, network, fence)
        await #expect(throws: status == 403 ? AuthenticationFailure.pendingSession : .unavailable) { try await transport.admit() }
        #expect(tokens.forced == 0); #expect(network.requests.count == 1); #expect(!transport.isAdmitted)
    }
}
@Test @MainActor func delayedAdmissionCannotUnlockSwitchedIdentity() async throws {
    let gate = Gate(), tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    network.hook = { _ in await gate.pause(); return try admission() }
    let transport = try transport(tokens, network, fence)
    let task = Task { try await transport.admit() }; try await gate.wait()
    tokens.currentIdentity = identity("user_B", session: "session_B"); _ = fence.activate(tokens.currentIdentity!)
    gate.release()
    await #expect(throws: AuthenticationFailure.changedSession) { try await task.value }
    #expect(!transport.isAdmitted)
}
@Test @MainActor func delayedTokenAfterLogoutCannotSendAnything() async throws {
    let gate = Gate(), tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    tokens.hook = { _ in await gate.pause(); return "unit-token" }
    let transport = try transport(tokens, network, fence)
    let task = Task { try await transport.admit() }; try await gate.wait()
    fence.lock(); tokens.currentIdentity = nil; gate.release()
    await #expect(throws: AuthenticationFailure.locked) { try await task.value }
    #expect(network.requests.isEmpty)
}
@Test @MainActor func delayedDataDiscardedAfterSameUserReAdmission() async throws {
    let gate = Gate(), tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    network.hook = { r in
        if r.url?.path == "/auth/v1/session" { return try admission() }
        await gate.pause(); return HTTPResult(status: 200, body: Data("{}".utf8))
    }
    let transport = try transport(tokens, network, fence); try await transport.admit()
    let old = Task { try await transport.send(request("/state")) }; try await gate.wait()
    transport.invalidateAdmission(); try await transport.admit(); gate.release()
    await #expect(throws: AuthenticationFailure.changedSession) { try await old.value }
    #expect(transport.isAdmitted)
}
@Test @MainActor func late401AfterAccountSwitchNeverRefreshesOrRetags() async throws {
    let gate = Gate(), tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    network.hook = { r in
        if r.url?.path == "/auth/v1/session" { return try admission() }
        await gate.pause(); return HTTPResult(status: 401, body: Data())
    }
    let transport = try transport(tokens, network, fence); try await transport.admit()
    let pending = Task { try await transport.send(request(body: Wire.encode(feedback()))) }; try await gate.wait()
    tokens.currentIdentity = identity("user_B", session: "session_B"); _ = fence.activate(tokens.currentIdentity!); gate.release()
    await #expect(throws: AuthenticationFailure.changedSession) { try await pending.value }
    #expect(tokens.forced == 0); #expect(network.requests.count == 2)
}
@Test @MainActor func simultaneous401CannotMultiplyRefreshesOrAcceptOlderAdmission() async throws {
    let gate = Gate(), tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    var firstMutation = true
    network.hook = { r in
        if r.url?.path == "/auth/v1/session" { return try admission() }
        if r.url?.path == "/preferences" { await gate.pause(); return HTTPResult(status: 401, body: Data()) }
        if firstMutation { firstMutation = false; return HTTPResult(status: 401, body: Data()) }
        return HTTPResult(status: 200, body: Data("{}".utf8))
    }
    let transport = try transport(tokens, network, fence); try await transport.admit()
    let old = Task { try await transport.send(request("/preferences", body: Data("{}".utf8))) }; try await gate.wait()
    _ = try await transport.send(request(body: Wire.encode(feedback()))); gate.release()
    await #expect(throws: AuthenticationFailure.changedSession) { try await old.value }
    #expect(tokens.forced == 1)
}
@Test @MainActor func bearerNeverGoesToAnotherOriginQueryOrAnonymousDemo() async throws {
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    let transport = try transport(tokens, network, fence); try await transport.admit()
    for url in ["http://127.0.0.1:8000/state", "https://another.example/state", "http://127.0.0.1:9876/state?token=x", "http://127.0.0.1:9876/demo"] {
        await #expect(throws: AuthenticationFailure.invalidDestination) { try await transport.send(URLRequest(url: URL(string: url)!)) }
    }
    #expect(network.requests.count == 1)
}
@Test @MainActor func missingOversizedAndUnsafeTokensLockWithoutNetwork() async throws {
    for token: String? in [nil, "", String(repeating: "x", count: 16_385), "bad token", "bad\r\nheader"] {
        let tokens = Tokens(), network = Network(), fence = AuthSessionFence(); tokens.hook = { _ in token }
        let transport = try transport(tokens, network, fence)
        await #expect(throws: AuthenticationFailure.invalidToken) { try await transport.admit() }
        #expect(network.requests.isEmpty); #expect(fence.identity == nil)
    }
}
@Test @MainActor func accountSwitchAndLogoutDeletePendingWithoutAnonymousAdoption() async throws {
    let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
    let cache = AccountCache(root: root)
    let storageA = try cache.activate(identity())
    let clientA = try CompanionClient(storage: storageA, transport: StubHTTP(state: fixture()))
    try await clientA.refresh(); try clientA.enqueue(PendingMutation(path: "/feedback", request: feedback(epoch: clientA.snapshot.state!.data_epoch)))
    #expect(clientA.snapshot.outbox.count == 1)
    let storageB = try cache.activate(identity("user_B", session: "session_B"))
    clientA.invalidateSession()
    #expect(!FileManager.default.fileExists(atPath: storageA.directory.path))
    #expect(try storageB.load().state == nil); #expect(try storageB.load().outbox.isEmpty)
    #expect(storageA.directory != storageB.directory)
    try cache.clearActive(); #expect(!FileManager.default.fileExists(atPath: storageB.directory.path))
    #expect(clientA.snapshot.state == nil); #expect(clientA.snapshot.outbox.isEmpty)
    #expect(throws: AuthenticationFailure.locked) { try clientA.enqueue(PendingMutation(path: "/feedback", request: feedback())) }
}
@Test @MainActor func endpointChangeNeverAdoptsSameUserCacheOrOutbox() async throws {
    let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
    let a = AccountCache(root: root, deployment: "http://127.0.0.1:9876")
    let client = try CompanionClient(storage: a.activate(identity()), transport: StubHTTP(state: fixture()))
    try await client.refresh(); try client.enqueue(PendingMutation(path: "/feedback", request: feedback(epoch: client.snapshot.state!.data_epoch)))
    let b = AccountCache(root: root, deployment: "http://127.0.0.1:9877")
    let storage = try b.activate(identity())
    #expect(try storage.load().state == nil); #expect(try storage.load().outbox.isEmpty)
}
@Test func accountNamespaceIncludesIssuerNotSessionOrRawPath() {
    #expect(identity().accountNamespace == identity(session: "new_session").accountNamespace)
    #expect(identity().accountNamespace != identity("user_B").accountNamespace)
    #expect(identity().accountNamespace != AuthIdentity(issuer: "https://different.example", subject: "user_A", sessionID: "session_A").accountNamespace)
    #expect(identity("../traversal").accountNamespace.count == 64)
    #expect(!identity("../traversal").accountNamespace.contains("/"))
}

@Test @MainActor func delayedUserAMutationCannotRepopulateUserBCache() async throws {
    let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
    let cache = AccountCache(root: root), gate = Gate(), tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    let stateA = try fixture()
    var stateB = try fixture(); stateB.observations = []; stateB.ledger = []; stateB.preferences.notes = "B only"
    network.hook = { r in
        if r.url?.path == "/auth/v1/session" { return try admission(tokens.currentIdentity!.subject) }
        if r.httpMethod == "POST" { await gate.pause(); return HTTPResult(status: 200, body: try Wire.encode(stateA)) }
        return HTTPResult(status: 200, body: try Wire.encode(tokens.currentIdentity?.subject == "user_A" ? stateA : stateB))
    }
    let first = try transport(tokens, network, fence); try await first.admit()
    let clientA = try CompanionClient(storage: cache.activate(identity()), transport: first, baseURL: origin, allowedPort: 9876, sessionValidator: { try first.validate() })
    try await clientA.refresh(); try clientA.enqueue(PendingMutation(path: "/feedback", request: feedback(epoch: stateA.data_epoch)))
    let old = Task { try await clientA.synchronize() }; try await gate.wait()
    fence.lock(); clientA.invalidateSession(); try cache.clearActive()
    tokens.currentIdentity = identity("user_B", session: "session_B")
    let second = try transport(tokens, network, fence); try await second.admit()
    let storageB = try cache.activate(tokens.currentIdentity!)
    let clientB = try CompanionClient(storage: storageB, transport: second, baseURL: origin, allowedPort: 9876, sessionValidator: { try second.validate() })
    try await clientB.refresh(); gate.release()
    await #expect(throws: (any Error).self) { try await old.value }
    #expect(clientA.snapshot.state == nil)
    #expect(clientB.snapshot.state?.observations.isEmpty == true)
    #expect(clientB.snapshot.state?.preferences.notes == "B only")
    #expect(try storageB.load().outbox.isEmpty)
    #expect(try storageB.load().state?.preferences.notes == "B only")
    let data = try Data(contentsOf: storageB.directory.appendingPathComponent("snapshot.json"))
    #expect(!String(decoding: data, as: UTF8.self).contains("unit-initial-token"))
}

@Test @MainActor func configuredHTTPSOriginPinsAdmissionAndEveryAPIRequest() async throws {
    let configured = try APIOrigin.url("https://api.example.com:8443/")
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    let transport = try AuthenticatedTransport(base: network, provider: tokens, fence: fence,
        lease: fence.activate(tokens.currentIdentity!), origin: configured)
    try await transport.admit()
    let state = try fixture()
    network.hook = { _ in HTTPResult(status: 200, body: try Wire.encode(state)) }
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let client = try CompanionClient(storage: storage, transport: transport, baseURL: configured, sessionValidator: { try transport.validate() })
    try await client.refresh()
    // Exercise the same client route builder for projections; this state-only double
    // intentionally fails projection decoding after recording the outbound URL.
    _ = try? await client.refreshExperience()
    _ = try? await client.refreshBanking()
    let paths = Set(network.requests.compactMap { $0.url?.path })
    #expect(paths.isSuperset(of: ["/auth/v1/session", "/state", "/experience/v1", "/banking/v1"]))
    #expect(network.requests.allSatisfy { $0.url?.host == "api.example.com" && $0.url?.scheme == "https" && $0.url?.port == 8443 })
    #expect(network.requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer unit-initial-token" })
    let count = network.requests.count, tokenCount = tokens.normal
    for destination in ["https://other.example/state", issuer + "/state", "https://api.example.com/state", "http://api.example.com:8443/state"] {
        await #expect(throws: AuthenticationFailure.invalidDestination) {
            try await transport.send(URLRequest(url: URL(string: destination)!))
        }
    }
    #expect(network.requests.count == count)
    #expect(tokens.normal == tokenCount)
}

@Test @MainActor func clerkCannotBeConfiguredAsBearerAPIDestination() throws {
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    #expect(throws: AuthenticationFailure.invalidDestination) {
        try AuthenticatedTransport(base: network, provider: tokens, fence: fence,
            lease: fence.activate(tokens.currentIdentity!), origin: URL(string: issuer)!)
    }
    #expect(tokens.normal == 0 && network.requests.isEmpty)
}

@Test @MainActor func httpsAdmissionRedirectNeverAdmitsOrFetchesData() async throws {
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    network.hook = { _ in HTTPResult(status: 302, body: Data()) }
    let transport = try AuthenticatedTransport(base: network, provider: tokens, fence: fence,
        lease: fence.activate(tokens.currentIdentity!), origin: try APIOrigin.url("https://api.example.com"))
    await #expect(throws: AuthenticationFailure.profileMismatch) { try await transport.admit() }
    #expect(!transport.isAdmitted && network.requests.count == 1)
}

@Test @MainActor func verifier503LeavesOriginalOutboxRetryableWithoutRefresh() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let tokens = Tokens(), network = Network(), fence = AuthSessionFence()
    let state = try fixture(); var unavailable = true
    network.hook = { r in
        if r.url?.path == "/auth/v1/session" { return try admission() }
        if r.httpMethod == "POST" && unavailable { return HTTPResult(status: 503, body: Data()) }
        return HTTPResult(status: 200, body: try Wire.encode(state))
    }
    let transport = try transport(tokens, network, fence); try await transport.admit()
    let client = try CompanionClient(storage: storage, transport: transport, baseURL: origin, allowedPort: 9876, sessionValidator: { try transport.validate() })
    try await client.refresh()
    let pending = try PendingMutation(path: "/feedback", request: feedback(epoch: state.data_epoch))
    try client.enqueue(pending)
    await #expect(throws: AuthenticationFailure.unavailable) { try await client.synchronize() }
    #expect(client.snapshot.outbox.first?.blockedCode == nil)
    #expect(client.snapshot.outbox.first?.body == pending.body); #expect(tokens.forced == 0)
    unavailable = false; try await client.synchronize()
    let sent = network.requests.filter { $0.httpMethod == "POST" }
    #expect(sent.count == 2); #expect(sent.allSatisfy { $0.httpBody == pending.body })
    #expect(client.snapshot.outbox.isEmpty)
}
