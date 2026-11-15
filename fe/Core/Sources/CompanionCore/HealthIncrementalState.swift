import Foundation

public struct HealthIncrementalCheckpoint: Codable, Equatable, Sendable {
    public let start: Date
    public let anchor: Data?
    public let terminalPending: Bool
    public init(start: Date, anchor: Data?, terminalPending: Bool) { self.start = start; self.anchor = anchor; self.terminalPending = terminalPending }
}
public struct HealthIncrementalAdmission: Codable, Equatable, Sendable {
    let anchor: Data
    let hasMore: Bool
    let digest: String
}
public struct HealthIncrementalStream: Codable, Equatable, Sendable {
    let epoch: String
    let streamID: String
    var nextSequence: Int
    var checkpoint: HealthIncrementalCheckpoint
    var lastAdmission: HealthIncrementalAdmission?
}

/// A background Health capability cannot send any general mutation or read reports.
@MainActor public final class HealthOnlyTransport: HTTPTransport {
    private let base: any HTTPTransport
    public init(base: any HTTPTransport) { self.base = base }
    public func send(_ request: URLRequest) async throws -> HTTPResult {
        let pair = (request.httpMethod ?? "GET", request.url?.path ?? "")
        guard pair == ("GET", "/health/v1/incremental/status") || pair == ("POST", "/health/v1/incremental") else { throw ClientFailure.invalidEndpoint }
        return try await base.send(request)
    }
}

/// Separate from foreground admission. Successful Health admission cannot unlock UI.
@MainActor public final class HealthBackgroundLease {
    private let fence = AuthSessionFence()
    public let transport: AuthenticatedTransport
    public init(identity: AuthIdentity, provider: any SessionTokenProvider, base: any HTTPTransport, origin: URL) throws {
        guard provider.currentIdentity == identity else { throw AuthenticationFailure.changedSession }
        let lease = fence.activate(identity)
        transport = try AuthenticatedTransport(base: base, provider: provider, fence: fence, lease: lease, origin: origin)
    }
    private var admission: Task<Void, Error>?
    public func admit() async throws {
        try Task.checkCancellation()
        // Sibling Health types share this authority. Re-admission would invalidate
        // their in-flight status requests and delta acknowledgements.
        if transport.isAdmitted { try transport.validate(); return }
        if let admission {
            try await admission.value
        } else {
            let task = Task { try await transport.admit() }
            admission = task
            defer { admission = nil }
            try await task.value
        }
        // Each waiter must still belong to the live session after suspension.
        try transport.validate()
    }
    public func validate() throws { try transport.validate() }
    public func stop() { admission?.cancel(); transport.cancel(); fence.lock() }
}
