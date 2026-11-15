import Foundation
import CryptoKit

public enum Wire {
    public static func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(value)
    }
    public static func decode<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T {
        try JSONDecoder().decode(type, from: data)
    }
}

public enum ClientFailure: Error, Equatable, Sendable {
    case busy, noState, invalidEndpoint, invalidResponse, expiredRequest, pendingChanges
    case rejected(String, Bool)
}

public struct PendingMutation: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let epoch: String
    public let path: String
    public let method: String
    public let body: Data
    public let createdAt: Date
    public var blockedCode: String? = nil
    public var blocksDisplay: Bool { path == "/data" || (path == "/demo" && (try? Wire.decode(DemoRequest.self, body).action) == "reset") }
    public init<T: Encodable>(path: String, method: String = "POST", request: T, now: Date = Date()) throws {
        let body = try Wire.encode(request)
        let methods = ["/health/v1/incremental": "POST", "/health/v1/samples": "POST", "/observations": "POST", "/feedback": "POST", "/preferences": "PUT", "/process": "POST", "/demo": "POST", "/data": "DELETE", "/experience/v1/answers": "POST"]
        guard methods[path] == method, body.count <= 1_048_576 else { throw ClientFailure.rejected("invalid_request", false) }
        // Round-trip through exact required-key/enum decoders before persisting a request.
        switch path {
        case "/health/v1/incremental": _ = try Wire.decode(HealthIncrementalRequest.self, body)
        case "/health/v1/samples": _ = try Wire.decode(HealthIngestRequest.self, body)
        case "/observations": _ = try Wire.decode(ObservationBatch.self, body)
        case "/feedback": _ = try Wire.decode(FeedbackRequest.self, body)
        case "/preferences": _ = try Wire.decode(PreferencesRequest.self, body)
        case "/process": _ = try Wire.decode(ProcessRequest.self, body)
        case "/demo": _ = try Wire.decode(DemoRequest.self, body)
        case "/data": _ = try Wire.decode(DeleteRequest.self, body)
        case "/experience/v1/answers": _ = try Wire.decode(ExperienceAnswerRequest.self, body)
        default: throw ClientFailure.rejected("invalid_request", false)
        }
        guard let json = try JSONSerialization.jsonObject(with: body) as? [String: Any],
              let id = json["request_id"] as? String, let epoch = json["data_epoch"] as? String else { throw ClientFailure.invalidResponse }
        self.id = id; self.epoch = epoch; self.path = path; self.method = method; self.body = body; createdAt = now
    }
}

public struct LocalSnapshot: Codable, Equatable, Sendable {
    public var state: State?
    public var outbox: [PendingMutation]
    public var healthStreams: [String: HealthIncrementalStream]?
    public var healthAuthorizationEpoch: String?
    public var healthCollectionPaused: Bool?
    public init(state: State? = nil, outbox: [PendingMutation] = []) { self.state = state; self.outbox = outbox }
    /// No retagging: the old queue and cached records die at the epoch boundary.
    public mutating func accept(_ incoming: State) {
        if state?.data_epoch != incoming.data_epoch { outbox.removeAll(); healthStreams = nil; healthAuthorizationEpoch = nil; healthCollectionPaused = nil }
        else if let state, state.revision > incoming.revision { return }
        state = incoming
    }
    public var hasGeneralPendingChanges: Bool { outbox.contains { $0.path != "/health/v1/incremental" } }
    public var hidesCachedData: Bool { outbox.contains(where: \.blocksDisplay) }
}

public struct ProtectedStorage: Sendable {
    public let directory: URL
    public init(directory: URL) { self.directory = directory }
    public func load() throws -> LocalSnapshot {
        let file = directory.appendingPathComponent("snapshot.json")
        guard FileManager.default.fileExists(atPath: file.path) else { return LocalSnapshot() }
        // A protected/unreadable/corrupt file must not be mistaken for an empty queue.
        return try Wire.decode(LocalSnapshot.self, Data(contentsOf: file))
    }
    public func save(_ snapshot: LocalSnapshot) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        var folder = directory
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try folder.setResourceValues(values)
        #if os(iOS)
        try fm.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: directory.path)
        #endif
        var file = directory.appendingPathComponent("snapshot.json")
        let data = try Wire.encode(snapshot)
        #if os(iOS)
        try data.write(to: file, options: [.atomic, .completeFileProtection])
        #else
        try data.write(to: file, options: .atomic)
        #endif
        try file.setResourceValues(values)
    }
}

public struct HTTPResult: Sendable {
    public let status: Int
    public let body: Data
    public let contentType: String?
    public init(status: Int, body: Data, contentType: String? = nil) { self.status = status; self.body = body; self.contentType = contentType }
}
public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> HTTPResult
}
/// A preview/test boundary enforced at transport, not merely by disabled controls.
public struct ReadOnlyTransport: HTTPTransport {
    private let base: any HTTPTransport
    public init(base: any HTTPTransport) { self.base = base }
    public func send(_ request: URLRequest) async throws -> HTTPResult {
        guard (request.httpMethod ?? "GET") == "GET", request.url?.path == "/state" else {
            throw ClientFailure.rejected("read_only_preview", false)
        }
        return try await base.send(request)
    }
}
private final class NoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
public final class URLSessionTransport: HTTPTransport, @unchecked Sendable {
    private let session: URLSession
    public init() {
        let config = URLSessionConfiguration.ephemeral
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil; config.httpCookieStorage = nil
        config.httpShouldSetCookies = false; config.urlCredentialStorage = nil
        config.timeoutIntervalForRequest = 35; config.timeoutIntervalForResource = 40
        session = URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }
    public func send(_ request: URLRequest) async throws -> HTTPResult {
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ClientFailure.invalidResponse }
        return HTTPResult(status: response.statusCode, body: data, contentType: response.value(forHTTPHeaderField: "Content-Type"))
    }
}

/// Main-actor serialization keeps state/outbox transitions atomic across suspension points.
/// A second action fails busy instead of racing a stale preference form or sensor revision.
@MainActor public final class CompanionClient {
    public private(set) var snapshot: LocalSnapshot
    public private(set) var busy = false
    private let storage: ProtectedStorage
    private let transport: any HTTPTransport
    private let baseURL: URL
    private let retryDelay: UInt64
    public private(set) var experienceSnapshot: ExperienceV1?
    /// Inventory and credential-bearing replies are never part of LocalSnapshot.
    public private(set) var bankingSnapshot: BankingOverview?
    private var bankingGeneration = 0
    private var bankingFlight: UUID?
    public func clearBankingSnapshot() { bankingSnapshot = nil }
    public func invalidateBankingWork() {
        bankingGeneration += 1; bankingFlight = nil; bankingSnapshot = nil
    }
    private var sessionActive = true
    private var healthSessionValidator: (@MainActor @Sendable () throws -> Void)?
    private let sessionValidator: @MainActor @Sendable () throws -> Void
    private var plaidGeneration = 0
    private var plaidFlight: UUID?
    public func invalidatePlaidWork() { plaidGeneration += 1; plaidFlight = nil }
    public init(storage: ProtectedStorage, transport: any HTTPTransport = URLSessionTransport(), baseURL: URL? = nil, allowedPort: Int? = nil, retryDelay: UInt64 = 500_000_000, sessionValidator: @escaping @MainActor @Sendable () throws -> Void = {}) throws {
        let baseURL = try baseURL.map { try APIOrigin.url($0.absoluteString) } ?? APIConfiguration.resolve()
        // Optional compatibility assertion for callers injecting test endpoints, never an override.
        if let allowedPort {
            guard (1...65535).contains(allowedPort), baseURL.port == allowedPort else { throw ClientFailure.invalidEndpoint }
        }
        self.storage = storage; self.transport = transport; self.baseURL = baseURL; self.retryDelay = retryDelay; self.sessionValidator = sessionValidator
        try sessionValidator()
        snapshot = try storage.load()
    }
    public func invalidateSession() {
        sessionActive = false; invalidatePlaidWork(); invalidateBankingWork(); snapshot = LocalSnapshot(); experienceSnapshot = nil
    }
    private func validateSession() throws {
        guard sessionActive else { throw AuthenticationFailure.locked }
        try sessionValidator()
    }
    private func persist(_ next: LocalSnapshot) throws {
        try validateSession()
        if snapshot.state?.data_epoch != next.state?.data_epoch { invalidateBankingWork() }
        if snapshot.state?.data_epoch != next.state?.data_epoch || (experienceSnapshot?.revision ?? -1) < (next.state?.revision ?? 0) { experienceSnapshot = nil }
        do { try storage.save(next) }
        catch { LaunchDiagnostic.failure(error, at: .persistence); throw error }
        snapshot = next
    }
    private func request(path: String, method: String = "GET", body: Data? = nil) -> URLRequest {
        var r = URLRequest(url: baseURL.appendingPathComponent(String(path.dropFirst())))
        r.httpMethod = method; r.httpBody = body
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { r.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return r
    }
    private func send(_ r: URLRequest) async throws -> Data {
        try validateSession()
        let stage: LaunchDiagnostic.Stage = r.url?.path == "/state" ? .stateRequest
            : r.url?.path == "/experience/v1" ? .experienceRequest : .otherRequest
        let response: HTTPResult
        do { response = try await transport.send(r) }
        catch { LaunchDiagnostic.failure(error, at: stage); throw error }
        try validateSession()
        if response.status == 200 { return response.body }
        LaunchDiagnostic.http(response.status, at: stage)
        let envelope = try LaunchDiagnostic.decode(APIErrorEnvelope.self, response.body, at: stage)
        throw ClientFailure.rejected(envelope.error.code, envelope.error.retryable)
    }
    /// Explicit manual review, never queued, scheduled or automatically transport-retried.
    public func reportNotificationStatus(registrationRequestID: String? = nil) async throws -> NotificationStatus {
        try validateSession()
        let epoch = snapshot.state?.data_epoch
        let body = try registrationRequestID.map { try Wire.encode(NotificationStatusRequest(schema_version: "1", registration_request_id: $0)) }
        let value = try Wire.decode(NotificationStatus.self, await send(request(path: body == nil ? "/report-notifications/v1" : "/report-notifications/v1/status", method: body == nil ? "GET" : "POST", body: body)))
        try validateSession()
        guard epoch == snapshot.state?.data_epoch, epoch == value.data_epoch else { throw ClientFailure.invalidResponse }
        return value
    }
    public func registerReportNotifications(_ body: NotificationRegister) async throws -> NotificationRegistration {
        try validateSession()
        guard snapshot.state?.data_epoch == body.data_epoch else { throw ClientFailure.invalidResponse }
        let value = try Wire.decode(NotificationRegistration.self, await send(request(path: "/report-notifications/v1/register", method: "POST", body: Wire.encode(body))))
        try validateSession()
        guard snapshot.state?.data_epoch == body.data_epoch, value.status.data_epoch == body.data_epoch else { throw ClientFailure.invalidResponse }
        return value
    }
    public func revokeReportNotifications(_ body: NotificationRevoke) async throws -> NotificationStatus {
        try validateSession()
        let value = try Wire.decode(NotificationStatus.self, await send(request(path: "/report-notifications/v1/revoke", method: "POST", body: Wire.encode(body))))
        try validateSession()
        return value
    }
    public func logoutReportNotifications() async throws -> NotificationLogoutResult {
        try validateSession()
        let body = NotificationLogout(schema_version: "1", request_id: UUID().uuidString)
        let value = try Wire.decode(NotificationLogoutResult.self, await send(request(path: "/report-notifications/v1/logout", method: "POST", body: Wire.encode(body))))
        try validateSession()
        return value
    }
    public func workflows(requestID: String? = nil) async throws -> WorkflowOverview {
        try validateSession()
        guard !busy, !snapshot.hasGeneralPendingChanges else { throw ClientFailure.busy }
        guard let epoch = snapshot.state?.data_epoch else { throw ClientFailure.noState }
        let body = try requestID.map { try Wire.encode(EnqueueRequest(schema_version: "1", request_id: $0, data_epoch: epoch)) }
        let value = try Wire.decode(WorkflowOverview.self, await send(request(path: requestID == nil ? "/workflows/v1" : "/workflows/v1/enqueue", method: requestID == nil ? "GET" : "POST", body: body)))
        try validateSession()
        guard epoch == snapshot.state?.data_epoch else { throw ClientFailure.invalidResponse }
        return value
    }
    public func patterns(requestID: String? = nil, fetchLatestBankData: Bool = false) async throws -> PatternOverview {
        try validateSession()
        guard !busy, !snapshot.hasGeneralPendingChanges else { throw ClientFailure.busy }
        let epoch = snapshot.state?.data_epoch
        let body = try requestID.map { try Wire.encode(PatternAnalyzeRequest(schema_version: "1", request_id: $0, data_epoch: epoch ?? "", fetch_latest_bank_data: fetchLatestBankData)) }
        let value = try Wire.decode(PatternOverview.self, await send(request(path: requestID == nil ? "/patterns/v1" : "/patterns/v1/analyze", method: requestID == nil ? "GET" : "POST", body: body)))
        try validateSession()
        guard epoch == snapshot.state?.data_epoch else { throw ClientFailure.invalidResponse }
        return value
    }
    public func clarifyPattern(runID: String, concernID: String, attribution: String) async throws -> PatternOverview {
        try validateSession()
        guard !busy, !snapshot.hasGeneralPendingChanges else { throw ClientFailure.busy }
        let epoch = snapshot.state?.data_epoch
        let body = try Wire.encode(PatternClarifyRequest(schema_version: "1", run_id: runID, concern_id: concernID, attribution: attribution))
        let value = try Wire.decode(PatternOverview.self, await send(request(path: "/patterns/v1/clarify", method: "POST", body: body)))
        guard epoch == snapshot.state?.data_epoch else { throw ClientFailure.invalidResponse }
        return value
    }
    public func savePatternBrief(runID: String, expectedBrief: String, text: String) async throws -> PatternOverview {
        try validateSession()
        guard !busy, !snapshot.hasGeneralPendingChanges else { throw ClientFailure.busy }
        let epoch = snapshot.state?.data_epoch
        let body = try Wire.encode(PatternBriefRequest(schema_version: "1", run_id: runID, expected_brief: expectedBrief, text: text))
        let value = try Wire.decode(PatternOverview.self, await send(request(path: "/patterns/v1/brief", method: "POST", body: body)))
        guard epoch == snapshot.state?.data_epoch else { throw ClientFailure.invalidResponse }
        return value
    }
    private func fetch() async throws {
        let incoming = try LaunchDiagnostic.decode(State.self, await send(request(path: "/state")), at: .stateDecode)
        if snapshot.state?.data_epoch != incoming.data_epoch { invalidatePlaidWork() }
        var next = snapshot; next.accept(incoming); try persist(next)
    }
    private func bankingResponse(_ outgoing: URLRequest, current: BankingVersion) async throws -> BankingOverview {
        try validateSession()
        let response = try await transport.send(outgoing)
        try validateSession()
        if response.status == 404 && outgoing.httpMethod == "GET" { throw BankingTransportFailure.profileUnavailable }
        return try BankingResponse.decode(response.body, statusCode: response.status, current: current)
    }
    private func fetchBanking() async throws -> BankingOverview {
        try validateSession(); bankingSnapshot = nil
        guard let state = snapshot.state else { throw ClientFailure.noState }
        let generation = bankingGeneration
        let version = BankingVersion(data_epoch: state.data_epoch, revision: state.revision)
        let value = try await bankingResponse(request(path: "/banking/v1"), current: version)
        guard value.result == nil else { throw ClientFailure.invalidResponse }
        if value.revision > (snapshot.state?.revision ?? -1) { try await fetch() }
        try validateSession()
        guard generation == bankingGeneration, snapshot.state?.data_epoch == value.data_epoch,
              value.revision == snapshot.state?.revision else { throw BankingFailure.staleResponse }
        bankingSnapshot = value
        return value
    }
    public func refreshBanking() async throws -> BankingOverview {
        try validateSession()
        guard !busy, bankingFlight == nil else { throw ClientFailure.busy }
        guard !snapshot.hasGeneralPendingChanges else { bankingSnapshot = nil; throw ClientFailure.pendingChanges }
        return try await fetchBanking()
    }
    /// One logical action, one immutable body and request ID. Nothing is queued or cached.
    /// Auth 401 replay is handled by the admitted transport; a one-use exchange is otherwise
    /// never automatically retried on an ambiguous transport/internal failure.
    public func banking(_ action: BankingRequest) async throws -> BankingOverview {
        try validateSession()
        guard !busy, bankingFlight == nil else { throw ClientFailure.busy }
        guard !snapshot.hasGeneralPendingChanges else { throw ClientFailure.pendingChanges }
        guard let state = snapshot.state, let current = bankingSnapshot,
              current.version == action.version, state.data_epoch == action.version.data_epoch,
              state.revision == action.version.revision else { throw BankingTransportFailure.staleAction }
        let generation = bankingGeneration, flight = UUID()
        bankingFlight = flight
        defer { if bankingFlight == flight { bankingFlight = nil } }
        func check() throws {
            try Task.checkCancellation(); try validateSession()
            guard generation == bankingGeneration, snapshot.state?.data_epoch == action.version.data_epoch,
                  !snapshot.hasGeneralPendingChanges else { throw BankingTransportFailure.interrupted }
        }
        let body = try action.body()
        let outgoing = request(path: "/banking/v1/actions", method: "POST", body: body)
        var acknowledged: BankingOverview?
        for attempt in 0..<3 {
            try check()
            do {
                acknowledged = try await bankingResponse(outgoing, current: action.version)
                break
            } catch {
                try check()
                if let rejected = error as? BankingAPIError {
                    if action.action.name == "exchange", rejected.code == .internal_error || (rejected.retryable && rejected.code != .busy) {
                        throw BankingTransportFailure.ambiguousExchange
                    }
                    guard [.busy, .internal_error].contains(rejected.code), rejected.retryable, attempt < 2 else { throw error }
                } else if error is URLError {
                    if action.action.name == "exchange" { throw BankingTransportFailure.ambiguousExchange }
                    guard attempt < 2 else { throw error }
                } else { throw error }
                try await Task.sleep(nanoseconds: retryDelay * UInt64(1 << attempt))
            }
        }
        try check()
        guard let acknowledged, acknowledged.result != nil else { throw ClientFailure.invalidResponse }
        // A receipt acknowledges an action, not the current inventory. GET State and inventory
        // before publishing; an old receipt cannot resurrect a removed account or old selection.
        try await fetch(); try check()
        _ = try await fetchBanking(); try check()
        return acknowledged
    }
    /// Memory-only, bounded retries. Each call is one logical action with one stable identity.
    /// A caller must carry the Link-origin epoch into exchange, never substitute the current one.
    public func plaid(_ action: PlaidAction, epoch: String, requestID: String = UUID().uuidString) async throws -> PlaidReply {
        guard !busy, plaidFlight == nil else { throw ClientFailure.busy }
        guard !snapshot.hasGeneralPendingChanges else { throw ClientFailure.pendingChanges }
        guard snapshot.state?.data_epoch == epoch else { throw PlaidFailure.staleWork }
        if action.name == "exchange" || action.name == "sync" {
            guard snapshot.state?.sources.contains(where: { $0.id == "plaid" && $0.enabled && $0.consent == "granted" }) == true else { throw ClientFailure.rejected("consent_required", false) }
        }
        let generation = plaidGeneration, flight = UUID()
        plaidFlight = flight
        defer { if plaidFlight == flight { plaidFlight = nil } }
        func check() throws {
            try Task.checkCancellation()
            guard generation == plaidGeneration, snapshot.state?.data_epoch == epoch,
                  !snapshot.hasGeneralPendingChanges else { throw PlaidFailure.staleWork }
        }
        let body = try action.body(epoch: epoch, requestID: requestID)
        let outgoing = request(path: "/connections/plaid", method: "POST", body: body)
        var response: Data?
        for attempt in 0..<3 {
            try check()
            do { response = try await send(outgoing); break }
            catch {
                try check()
                // Never repeat an exchange with an ambiguous transport outcome. Reconnect via fresh Link.
                if action.name == "exchange" {
                    if error is URLError { throw PlaidFailure.ambiguousExchange }
                    if case ClientFailure.rejected(let code, let retryable) = error, retryable && code != "busy" {
                        throw PlaidFailure.ambiguousExchange
                    }
                }
                let retry: Bool
                if case ClientFailure.rejected(_, let retryable) = error { retry = retryable }
                else { retry = error is URLError }
                guard retry, attempt < 2 else { throw error }
                try await Task.sleep(nanoseconds: retryDelay * UInt64(1 << attempt))
            }
        }
        try check()
        guard let response else { throw ClientFailure.invalidResponse }
        let reply = try PlaidReply.decode(response, action: action, requestID: requestID, epoch: epoch)
        // Revalidate against current server state before accepting a replay or exposing a Link token.
        let fresh = try Wire.decode(State.self, await send(request(path: "/state")))
        try check()
        if fresh.data_epoch != epoch {
            invalidatePlaidWork()
            var next = snapshot; next.accept(fresh); try persist(next)
            throw PlaidFailure.staleWork
        }
        var next = snapshot; next.accept(reply.state); next.accept(fresh); try persist(next)
        if action.name != "disconnect" {
            guard fresh.sources.contains(where: { $0.id == "plaid" && $0.enabled && $0.consent == "granted" }) else {
                invalidatePlaidWork(); throw PlaidFailure.staleWork
            }
        }
        return reply
    }
    public func refreshExperience() async throws -> ExperienceV1 {
        let incoming = try LaunchDiagnostic.decode(ExperienceV1.self, await send(request(path: "/experience/v1")), at: .experienceDecode)
        try validateSession()
        guard incoming.data_epoch == snapshot.state?.data_epoch else {
            experienceSnapshot = nil; throw ClientFailure.rejected("stale_experience", false)
        }
        if let existing = experienceSnapshot, existing.data_epoch == incoming.data_epoch, existing.revision >= (snapshot.state?.revision ?? 0), existing.revision > incoming.revision { return existing }
        guard incoming.revision >= (snapshot.state?.revision ?? 0) else { throw ClientFailure.rejected("stale_experience", false) }
        experienceSnapshot = incoming
        return incoming
    }
    public func discardObsoleteAnswers() throws {
        var next = snapshot
        next.outbox.removeAll { $0.path == "/experience/v1/answers" && ["stale_experience", "obsolete_pattern"].contains($0.blockedCode ?? "") }
        try persist(next)
    }
    public func refresh() async throws {
        guard !busy else { throw ClientFailure.busy }; busy = true; defer { busy = false }
        try await fetch()
    }
    public func enqueue(_ mutation: PendingMutation, replacingQueue: Bool = false) throws {
        try validateSession()
        guard !busy else { throw ClientFailure.busy }
        guard let state = snapshot.state else { throw ClientFailure.noState }
        guard mutation.epoch == state.data_epoch else { throw ClientFailure.rejected("epoch_mismatch", false) }
        if mutation.blocksDisplay { invalidatePlaidWork() }
        var next = snapshot
        if replacingQueue { next.outbox.removeAll { $0.path != "/health/v1/incremental" } }
        if let existing = next.outbox.first(where: { $0.id == mutation.id }) {
            guard existing == mutation else { throw ClientFailure.rejected("idempotency_conflict", false) }
            return
        }
        if mutation.path == "/health/v1/samples" && incrementalHealthHasState { throw ClientFailure.rejected("incremental_cutover", false) }
        if mutation.blocksDisplay { next.outbox.insert(mutation, at: 0) }
        else if mutation.path != "/health/v1/incremental", let firstHealth = next.outbox.firstIndex(where: { $0.path == "/health/v1/incremental" }) { next.outbox.insert(mutation, at: firstHealth) }
        else { next.outbox.append(mutation) }
        try persist(next)
    }
    public func discardPending() throws {
        guard !busy else { throw ClientFailure.busy }
        var next = snapshot; next.outbox.removeAll { $0.path != "/health/v1/incremental" }; try persist(next)
    }
    public func synchronize(now: Date = Date()) async throws {
        guard !busy else { throw ClientFailure.busy }; busy = true; defer { busy = false }
        // Refresh first: another client may have erased/reset the server while we were offline.
        try await fetch()
        while let item = snapshot.outbox.first {
            if item.path == "/health/v1/incremental", snapshot.state?.sources.contains(where: { $0.id == "healthkit" && $0.enabled && $0.consent == "granted" }) != true { return }
            guard item.epoch == snapshot.state?.data_epoch else { throw ClientFailure.rejected("epoch_mismatch", false) }
            guard item.path == "/health/v1/incremental" || now.timeIntervalSince(item.createdAt) < 90 * 86_400 else {
                var next = snapshot; next.outbox.removeAll { $0.path != "/health/v1/incremental" }; try persist(next); throw ClientFailure.expiredRequest
            }
            if let code = item.blockedCode { throw ClientFailure.rejected(code, false) }
            let r = request(path: item.path, method: item.method, body: item.body)
            var result: Data?
            for attempt in 0..<3 {
                do { result = try await send(r); break }
                catch {
                    let retry: Bool
                    if let failure = error as? ClientFailure {
                        if case .rejected(_, let retryable) = failure { retry = retryable } else { retry = false }
                    } else { retry = error is URLError }
                    // Auth/service gates do not poison an otherwise valid queued mutation.
                    // A later admitted retry must retain the original body/identity unchanged.
                    if error is AuthenticationFailure { throw error }
                    if !retry {
                        try validateSession()
                        var next = snapshot
                        let code: String
                        if case ClientFailure.rejected(let rejectedCode, _) = error { code = rejectedCode }
                        else { code = "invalid_response" }
                        guard let index = next.outbox.firstIndex(where: { $0.id == item.id }) else { throw error }
                        next.outbox[index].blockedCode = code
                        try persist(next)
                        throw error
                    }
                    if attempt == 2 { throw error }
                    try await Task.sleep(nanoseconds: retryDelay * UInt64(1 << attempt))
                }
            }
            guard let result else { throw ClientFailure.invalidResponse }
            try validateSession()
            var next = snapshot
            var receivedExperience: ExperienceV1?
            next.outbox.removeAll { $0.id == item.id }
            if item.path == "/health/v1/incremental" {
                try validateHealthReceipt(HTTPResult(status: 200, body: result), item: item)
            } else if item.path == "/experience/v1/answers" {
                let returned = try Wire.decode(ExperienceV1.self, result)
                guard returned.data_epoch == item.epoch else { throw ClientFailure.rejected("stale_experience", false) }
                // Preserve the server response only after current state confirms it is not an old receipt.
                receivedExperience = returned
            } else {
                let returned: State
                if item.path == "/observations" { returned = try Wire.decode(IngestionResponse.self, result).state }
                else { returned = try Wire.decode(State.self, result) }
                next.accept(returned)
            }
            try persist(next)
            // A replay may return an old receipt; GET gives the current committed projection.
            try await fetch()
            if let receivedExperience, receivedExperience.data_epoch == snapshot.state?.data_epoch,
               receivedExperience.revision >= (snapshot.state?.revision ?? 0),
               receivedExperience.revision >= (experienceSnapshot?.revision ?? 0) {
                experienceSnapshot = receivedExperience
            }
        }
    }
}

extension CompanionClient {
    public func setHealthSessionValidator(_ validate: @escaping @MainActor @Sendable () throws -> Void) { healthSessionValidator = validate }
    private func persistHealth(_ next: LocalSnapshot) throws {
        guard sessionActive else { throw AuthenticationFailure.locked }
        try (healthSessionValidator ?? sessionValidator)()
        guard snapshot.state?.data_epoch == next.state?.data_epoch else { throw ClientFailure.invalidResponse }
        try storage.save(next); snapshot = next
    }
    public func incrementalHealthStatus() async throws -> HealthIncrementalStatus {
        try validateSession()
        let result = try await transport.send(request(path: "/health/v1/incremental/status", method: "GET", body: nil))
        return try healthStatus(result)
    }
    public func backgroundHealthStatus(using background: any HTTPTransport) async throws -> HealthIncrementalStatus {
        guard sessionActive, snapshot.state != nil else { throw AuthenticationFailure.locked }
        return try healthStatus(await background.send(request(path: "/health/v1/incremental/status", method: "GET", body: nil)))
    }
    private func healthStatus(_ result: HTTPResult) throws -> HealthIncrementalStatus {
        guard sessionActive, result.status == 200 else { throw ClientFailure.invalidResponse }
        let status = try Wire.decode(HealthIncrementalStatus.self, result.body)
        guard status.data_epoch == snapshot.state?.data_epoch else { throw ClientFailure.invalidResponse }
        return status
    }
    public var incrementalHealthHasState: Bool { !(snapshot.healthStreams ?? [:]).isEmpty }
    public func setHealthCollectionPaused(_ paused: Bool) throws {
        try validateSession(); var next = snapshot; next.healthCollectionPaused = paused; try persist(next)
    }
    public func recordHealthAuthorizationRequested() throws {
        try validateSession(); var next = snapshot; next.healthAuthorizationEpoch = snapshot.state?.data_epoch; try persistHealth(next)
    }
    public var healthAuthorizationRequestedOnDevice: Bool { snapshot.state?.data_epoch != nil && snapshot.healthAuthorizationEpoch == snapshot.state?.data_epoch }
    public func validateHealthJournal(_ status: HealthIncrementalStatus) throws {
        guard sessionActive, snapshot.state?.data_epoch == status.data_epoch, status.enabled, status.schema_ready, status.baseline_supported, status.source_allowed else { throw ClientFailure.invalidResponse }
        guard !snapshot.hasGeneralPendingChanges else { throw ClientFailure.busy }
        for remote in status.streams {
            let local = snapshot.healthStreams?[remote.hk_type]
            if let id = remote.stream_id {
                guard let local, local.epoch == status.data_epoch, local.streamID == id,
                      remote.next_sequence <= local.nextSequence else { throw ClientFailure.invalidResponse }
            } else if let local, local.nextSequence > 0 {
                guard snapshot.outbox.contains(where: { item in
                    guard item.path == "/health/v1/incremental", let body = try? Wire.decode(HealthIncrementalRequest.self,item.body) else { return false }
                    return body.hk_type == remote.hk_type && body.stream_id == local.streamID && body.sequence == 0
                }) else { throw ClientFailure.invalidResponse }
            }
        }
    }
    public func healthCheckpoint(type: String) -> HealthIncrementalCheckpoint? { snapshot.healthStreams?[type]?.checkpoint }
    public func saveHealthCheckpoint(type: String, checkpoint: HealthIncrementalCheckpoint, epoch: String) throws {
        guard sessionActive, snapshot.state?.data_epoch == epoch else { throw AuthenticationFailure.locked }
        var next = snapshot
        var stream = next.healthStreams?[type] ?? HealthIncrementalStream(epoch: epoch, streamID: UUID().uuidString.lowercased(), nextSequence: 0, checkpoint: checkpoint, lastAdmission: nil)
        guard stream.epoch == epoch, stream.checkpoint.start == checkpoint.start,
              stream.checkpoint.anchor == checkpoint.anchor else { throw ClientFailure.invalidResponse }
        // Anchor progression happens only in the same atomic write as its typed outbox row.
        stream.checkpoint = checkpoint
        if next.healthStreams == nil { next.healthStreams = [:] }
        next.healthStreams?[type] = stream; try persistHealth(next)
    }
    public func admitHealthDelta(type: String, epoch: String, samples: [IncrementalSample], deletedUUIDs: [String], anchor: Data, hasMore: Bool) throws {
        guard sessionActive, snapshot.state?.data_epoch == epoch, !anchor.isEmpty,
              var stream = snapshot.healthStreams?[type], stream.epoch == epoch else { throw ClientFailure.invalidResponse }
        guard !snapshot.outbox.contains(where: { $0.blocksDisplay }) else { throw ClientFailure.busy }
        let body = HealthIncrementalRequest(schema_version: "1", request_id: UUID().uuidString.lowercased(), data_epoch: epoch, hk_type: type, stream_id: stream.streamID, sequence: stream.nextSequence, has_more: hasMore, samples: samples, deleted_uuids: deletedUUIDs)
        var identity = body; identity.request_id = "00000000-0000-0000-0000-000000000000"; identity.sequence = 0
        let digest = SHA256.hash(data: try Wire.encode(identity)).map { String(format: "%02x", $0) }.joined()
        let admission = HealthIncrementalAdmission(anchor: anchor, hasMore: hasMore, digest: digest)
        if stream.lastAdmission == admission { return }
        guard snapshot.outbox.count < 512 else { throw ClientFailure.busy }
        let mutation = try PendingMutation(path: "/health/v1/incremental", request: body)
        stream.nextSequence += 1; stream.lastAdmission = admission
        stream.checkpoint = HealthIncrementalCheckpoint(start: stream.checkpoint.start, anchor: anchor, terminalPending: hasMore)
        var next = snapshot; next.healthStreams?[type] = stream; next.outbox.append(mutation)
        try persistHealth(next) // One protected atomic file: no advanced anchor without the data obligation.
    }
    private func validateHealthReceipt(_ result: HTTPResult, item: PendingMutation) throws {
        guard sessionActive, result.status == 200 else { throw ClientFailure.invalidResponse }
        let expected = try Wire.decode(HealthIncrementalRequest.self, item.body)
        let received = try Wire.decode(HealthIncrementalReceipt.self, result.body)
        guard received.request_id == expected.request_id, received.data_epoch == expected.data_epoch,
              received.data_epoch == snapshot.state?.data_epoch, received.stream_id == expected.stream_id,
              received.sequence == expected.sequence, received.hk_type == expected.hk_type else { throw ClientFailure.invalidResponse }
    }
    public func drainHealthDeltas(using background: any HTTPTransport) async throws {
        guard sessionActive else { throw AuthenticationFailure.locked }
        guard !busy else { throw ClientFailure.busy }
        busy = true; defer { busy = false }
        while let item = snapshot.outbox.first {
            guard item.path == "/health/v1/incremental" else { return } // Never send general queued writes under the background lease.
            let result = try await background.send(request(path: item.path, method: item.method, body: item.body))
            try validateHealthReceipt(result, item: item)
            guard snapshot.outbox.first == item else { throw AuthenticationFailure.locked }
            var next = snapshot; next.outbox.removeFirst(); try persistHealth(next)
        }
    }
}
