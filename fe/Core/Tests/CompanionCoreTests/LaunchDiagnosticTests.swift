import Foundation
import Testing
@testable import CompanionCore

private final class DiagnosticEvents: @unchecked Sendable {
    private let lock = NSLock()
    private var events: [LaunchDiagnostic.Event] = []
    func append(_ event: LaunchDiagnostic.Event) { lock.withLock { events.append(event) } }
    var values: [LaunchDiagnostic.Event] { lock.withLock { events } }
}
private struct PrivateDiagnosticError: Error, CustomStringConvertible {
    var description: String { "DO_NOT_LOG_PRIVATE_SENTINEL" }
}
private struct PrivateDiagnosticKey: CodingKey {
    var stringValue = "DO_NOT_LOG_PRIVATE_SENTINEL"
    var intValue: Int? { nil }
    init() {}
    init?(stringValue: String) { return nil }
    init?(intValue: Int) { return nil }
}
private struct DiagnosticHTTP: HTTPTransport {
    let status: Int
    let body: Data
    func send(_ request: URLRequest) async throws -> HTTPResult { HTTPResult(status: status, body: body) }
}

@Test func launchDiagnosticCategoriesNeverRenderPrivateErrorMaterial() {
    let context = DecodingError.Context(codingPath: [PrivateDiagnosticKey()], debugDescription: "DO_NOT_LOG_PRIVATE_SENTINEL", underlyingError: PrivateDiagnosticError())
    let cases: [(any Error, LaunchDiagnostic.Category)] = [
        (ClientFailure.busy, .busy), (ClientFailure.rejected("DO_NOT_LOG_PRIVATE_SENTINEL", true), .rejected),
        (ClientFailure.pendingChanges, .pendingChanges), (ClientFailure.invalidResponse, .invalidResponse),
        (ClientFailure.noState, .clientOther), (AuthenticationFailure.locked, .authentication),
        (URLError(.notConnectedToInternet, userInfo: [NSLocalizedDescriptionKey: "DO_NOT_LOG_PRIVATE_SENTINEL"]), .transport),
        (DecodingError.dataCorrupted(context), .decoding), (DecodingError.keyNotFound(PrivateDiagnosticKey(), context), .decoding),
        (DecodingError.typeMismatch(String.self, context), .decoding), (DecodingError.valueNotFound(String.self, context), .decoding),
        (EncodingError.invalidValue("DO_NOT_LOG_PRIVATE_SENTINEL", .init(codingPath: [PrivateDiagnosticKey()], debugDescription: "DO_NOT_LOG_PRIVATE_SENTINEL")), .encoding),
        (CancellationError(), .cancelled), (NSError(domain: NSCocoaErrorDomain, code: 1, userInfo: [NSFilePathErrorKey: "DO_NOT_LOG_PRIVATE_SENTINEL"]), .storage),
        (PrivateDiagnosticError(), .unknown)
    ]
    let events = DiagnosticEvents()
    LaunchDiagnostic.$sink.withValue({ events.append($0) }) {
        LaunchDiagnostic.$enabled.withValue(true) {
            for (error, expected) in cases {
                #expect(LaunchDiagnostic.category(error) == expected)
                LaunchDiagnostic.failure(error, at: .synchronize)
            }
        }
    }
    #expect(events.values.map(\.line) == cases.map { "launch-v1 stage=synchronize category=\($0.1.rawValue)" })
    #expect(events.values.allSatisfy { !$0.line.contains("DO_NOT_LOG_PRIVATE_SENTINEL") })
}

@Test func launchDiagnosticDisabledByDefaultAndRestoredAfterScope() {
    let events = DiagnosticEvents()
    LaunchDiagnostic.$sink.withValue({ events.append($0) }) {
        LaunchDiagnostic.failure(ClientFailure.busy, at: .synchronize)
        LaunchDiagnostic.$enabled.withValue(true) { LaunchDiagnostic.failure(ClientFailure.busy, at: .synchronize) }
        LaunchDiagnostic.failure(ClientFailure.busy, at: .synchronize)
    }
    #expect(events.values.map(\.line) == ["launch-v1 stage=synchronize category=busy"])
}

@Test @MainActor func launchDiagnosticMalformedStateKeepsSafeMessageAndBusyCleanup() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let client = try CompanionClient(storage: storage, transport: DiagnosticHTTP(status: 200, body: Data("{\"DO_NOT_LOG_PRIVATE_SENTINEL\":1}".utf8)))
    let model = AppModel(client: client, accountNamespace: "synthetic-test")
    let events = DiagnosticEvents()
    await LaunchDiagnostic.$sink.withValue({ events.append($0) }) { await model.refresh() }
    #expect(events.values.map(\.line) == ["launch-v1 stage=stateDecode category=decoding", "launch-v1 stage=synchronize category=decoding"])
    #expect(model.message == "That action couldn’t finish safely. Refresh and try again.")
    #expect(!model.connected && !model.busy && !client.busy)
    #expect(client.snapshot.state == nil)
}

@Test @MainActor func launchDiagnosticHTTPFailurePrecedesMalformedEnvelopeWithoutPayload() async throws {
    let client = try CompanionClient(storage: temporaryStorage(), transport: DiagnosticHTTP(status: 503, body: Data("DO_NOT_LOG_PRIVATE_SENTINEL".utf8)))
    let events = DiagnosticEvents()
    await LaunchDiagnostic.$sink.withValue({ events.append($0) }) {
        await LaunchDiagnostic.$enabled.withValue(true) {
            do { try await client.synchronize(); Issue.record("Expected decoding failure") }
            catch { #expect(error is DecodingError) }
        }
    }
    #expect(events.values.map(\.line) == ["launch-v1 stage=stateRequest category=httpServerError", "launch-v1 stage=stateRequest category=decoding"])
    #expect(!client.busy)
}

@Test @MainActor func launchDiagnosticPersistenceFailureLeavesSnapshotUnpublished() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let client = try CompanionClient(storage: storage, transport: DiagnosticHTTP(status: 200, body: Wire.encode(fixture())))
    // Block directory creation using a disposable test file, not any existing app data.
    try Data().write(to: storage.directory)
    let model = AppModel(client: client, accountNamespace: "synthetic-test")
    let events = DiagnosticEvents()
    await LaunchDiagnostic.$sink.withValue({ events.append($0) }) { await model.refresh() }
    #expect(events.values.contains { $0.stage == .persistence && $0.category == .storage })
    #expect(client.snapshot.state == nil && !client.busy && !model.connected)
    #expect(model.message == "That action couldn’t finish safely. Refresh and try again.")
}

@Test @MainActor func launchDiagnosticAuthenticationFailureIsNotReclassifiedOrExposed() async throws {
    let client = try CompanionClient(storage: temporaryStorage(), transport: DiagnosticHTTP(status: 200, body: Data()))
    client.invalidateSession()
    let model = AppModel(client: client, accountNamespace: "synthetic-test")
    let events = DiagnosticEvents()
    await LaunchDiagnostic.$sink.withValue({ events.append($0) }) { await model.refresh() }
    #expect(events.values.map(\.line) == ["launch-v1 stage=synchronize category=authentication"])
    #expect(model.message == "Sign in again to access your records.")
    #expect(!client.busy && !model.connected)
}

@MainActor private final class DiagnosticGateHTTP: HTTPTransport {
    var started = false
    private var continuation: CheckedContinuation<Void, Never>?
    func send(_ request: URLRequest) async throws -> HTTPResult {
        await withCheckedContinuation { continuation = $0; started = true }
        return HTTPResult(status: 200, body: Data("{}".utf8))
    }
    func release() { continuation?.resume(); continuation = nil }
}
@Test @MainActor func launchDiagnosticConcurrentSynchronizationReportsBusyWithoutChangingFlight() async throws {
    let transport = DiagnosticGateHTTP()
    let client = try CompanionClient(storage: temporaryStorage(), transport: transport)
    let first = Task { try? await client.refresh() }
    while !transport.started { await Task.yield() }
    let events = DiagnosticEvents()
    let model = AppModel(client: client, accountNamespace: "synthetic-test")
    await LaunchDiagnostic.$sink.withValue({ events.append($0) }) { await model.refresh() }
    #expect(events.values.map(\.line) == ["launch-v1 stage=synchronize category=busy"])
    #expect(client.busy && !model.busy && !model.connected)
    transport.release(); await first.value
    #expect(!client.busy)
}

@Test @MainActor func launchDiagnosticExperienceDecodeBoundaryPreservesThrownType() async throws {
    let client = try CompanionClient(storage: temporaryStorage(), transport: DiagnosticHTTP(status: 200, body: Data("{}".utf8)))
    let events = DiagnosticEvents()
    await LaunchDiagnostic.$sink.withValue({ events.append($0) }) {
        await LaunchDiagnostic.$enabled.withValue(true) {
            do { _ = try await client.refreshExperience(); Issue.record("Expected decode failure") }
            catch { #expect(error is DecodingError) }
        }
    }
    #expect(events.values.map(\.line) == ["launch-v1 stage=experienceDecode category=decoding"])
}

private struct DiagnosticOverviewHTTP: HTTPTransport {
    let state: Data
    let experience: Data
    init() throws {
        experience = try Data(contentsOf: Bundle.module.url(forResource: "experience.example", withExtension: "json", subdirectory: "Fixtures")!)
        let overview = try Wire.decode(ExperienceV1.self, experience)
        var value = try fixture(); value.data_epoch = overview.data_epoch; value.revision = overview.revision
        state = try Wire.encode(value)
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        HTTPResult(status: 200, body: request.url?.path == "/state" ? state
            : request.url?.path == "/experience/v1" ? experience : Data("{}".utf8))
    }
}
#if os(macOS)
@Test @MainActor func launchDiagnosticReminderFailureAndSuccessfulRefreshPreserveBehavior() async throws {
    for fail in [false, true] {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let client = try CompanionClient(storage: storage, transport: DiagnosticOverviewHTTP())
        let model = AppModel(client: client, accountNamespace: "synthetic-test")
        let events = DiagnosticEvents()
        await LaunchDiagnostic.$sink.withValue({ events.append($0) }) {
            await LocalReminders.$diagnosticReconcileFailure.withValue(fail) { await model.refresh() }
        }
        if fail {
            #expect(events.values.last?.line == "launch-v1 stage=reminders category=unknown")
            #expect(model.message == "That action couldn’t finish safely. Refresh and try again.")
            #expect(!model.connected && model.experience == nil)
        } else {
            #expect(model.connected && model.experience != nil && model.message == nil)
        }
        #expect(!client.busy && !model.busy)
    }
}
#endif
