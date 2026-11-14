import Foundation

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
        let methods = ["/observations": "POST", "/feedback": "POST", "/preferences": "PUT", "/process": "POST", "/demo": "POST", "/data": "DELETE"]
        guard methods[path] == method, body.count <= 1_048_576 else { throw ClientFailure.rejected("invalid_request", false) }
        // Round-trip through exact required-key/enum decoders before persisting a request.
        switch path {
        case "/observations": _ = try Wire.decode(ObservationBatch.self, body)
        case "/feedback": _ = try Wire.decode(FeedbackRequest.self, body)
        case "/preferences": _ = try Wire.decode(PreferencesRequest.self, body)
        case "/process": _ = try Wire.decode(ProcessRequest.self, body)
        case "/demo": _ = try Wire.decode(DemoRequest.self, body)
        case "/data": _ = try Wire.decode(DeleteRequest.self, body)
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
    public init(state: State? = nil, outbox: [PendingMutation] = []) { self.state = state; self.outbox = outbox }
    /// No retagging: the old queue and cached records die at the epoch boundary.
    public mutating func accept(_ incoming: State) {
        if state?.data_epoch != incoming.data_epoch { outbox.removeAll() }
        else if let state, state.revision > incoming.revision { return }
        state = incoming
    }
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
    public init(status: Int, body: Data) { self.status = status; self.body = body }
}
public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> HTTPResult
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
        config.urlCache = nil; config.httpCookieStorage = nil
        config.httpShouldSetCookies = false; config.urlCredentialStorage = nil
        config.timeoutIntervalForRequest = 35; config.timeoutIntervalForResource = 40
        session = URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }
    public func send(_ request: URLRequest) async throws -> HTTPResult {
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ClientFailure.invalidResponse }
        return HTTPResult(status: response.statusCode, body: data)
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
    public init(storage: ProtectedStorage, transport: any HTTPTransport = URLSessionTransport(), baseURL: URL = URL(string: "http://127.0.0.1:8000")!, retryDelay: UInt64 = 500_000_000) throws {
        guard baseURL.scheme == "http", baseURL.host == "127.0.0.1", baseURL.port == 8000,
              baseURL.user == nil, baseURL.password == nil, baseURL.query == nil, baseURL.fragment == nil,
              baseURL.path.isEmpty || baseURL.path == "/" else { throw ClientFailure.invalidEndpoint }
        self.storage = storage; self.transport = transport; self.baseURL = baseURL; self.retryDelay = retryDelay
        snapshot = try storage.load()
    }
    private func persist(_ next: LocalSnapshot) throws { try storage.save(next); snapshot = next }
    private func request(path: String, method: String = "GET", body: Data? = nil) -> URLRequest {
        var r = URLRequest(url: baseURL.appendingPathComponent(String(path.dropFirst())))
        r.httpMethod = method; r.httpBody = body
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { r.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return r
    }
    private func send(_ r: URLRequest) async throws -> Data {
        let response = try await transport.send(r)
        if response.status == 200 { return response.body }
        let envelope = try Wire.decode(APIErrorEnvelope.self, response.body)
        throw ClientFailure.rejected(envelope.error.code, envelope.error.retryable)
    }
    private func fetch() async throws {
        let incoming = try Wire.decode(State.self, await send(request(path: "/state")))
        var next = snapshot; next.accept(incoming); try persist(next)
    }
    public func refresh() async throws {
        guard !busy else { throw ClientFailure.busy }; busy = true; defer { busy = false }
        try await fetch()
    }
    public func enqueue(_ mutation: PendingMutation, replacingQueue: Bool = false) throws {
        guard !busy else { throw ClientFailure.busy }
        guard let state = snapshot.state else { throw ClientFailure.noState }
        guard mutation.epoch == state.data_epoch else { throw ClientFailure.rejected("epoch_mismatch", false) }
        var next = snapshot
        if replacingQueue { next.outbox.removeAll() }
        if let existing = next.outbox.first(where: { $0.id == mutation.id }) {
            guard existing == mutation else { throw ClientFailure.rejected("idempotency_conflict", false) }
            return
        }
        next.outbox.append(mutation); try persist(next)
    }
    public func discardPending() throws {
        guard !busy else { throw ClientFailure.busy }
        var next = snapshot; next.outbox.removeAll(); try persist(next)
    }
    public func synchronize(now: Date = Date()) async throws {
        guard !busy else { throw ClientFailure.busy }; busy = true; defer { busy = false }
        // Refresh first: another client may have erased/reset the server while we were offline.
        try await fetch()
        while let item = snapshot.outbox.first {
            guard item.epoch == snapshot.state?.data_epoch else { throw ClientFailure.rejected("epoch_mismatch", false) }
            guard now.timeIntervalSince(item.createdAt) < 90 * 86_400 else {
                var next = snapshot; next.outbox.removeAll(); try persist(next); throw ClientFailure.expiredRequest
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
                    if !retry {
                        var next = snapshot
                        let code: String
                        if case ClientFailure.rejected(let rejectedCode, _) = error { code = rejectedCode }
                        else { code = "invalid_response" }
                        next.outbox[0].blockedCode = code
                        try persist(next)
                        throw error
                    }
                    if attempt == 2 { throw error }
                    try await Task.sleep(nanoseconds: retryDelay * UInt64(1 << attempt))
                }
            }
            guard let result else { throw ClientFailure.invalidResponse }
            let returned: State
            if item.path == "/observations" { returned = try Wire.decode(IngestionResponse.self, result).state }
            else { returned = try Wire.decode(State.self, result) }
            var next = snapshot
            next.outbox.removeAll { $0.id == item.id }
            next.accept(returned); try persist(next)
            // A replay may return an old receipt; GET gives the current committed projection.
            try await fetch()
        }
    }
}
