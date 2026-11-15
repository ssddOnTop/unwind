import Foundation
import CryptoKit

public enum AuthenticationFailure: Error, Equatable, Sendable {
    case locked, changedSession, unauthorized, pendingSession, unavailable, invalidToken, invalidDestination, profileMismatch, notAdmitted
}

/// SDK identity selects isolated pending-upload metadata only. The server verifies JWT authority.
public struct AuthIdentity: Equatable, Hashable, Sendable {
    public let issuer: String
    public let subject: String
    public let sessionID: String
    public init(issuer: String, subject: String, sessionID: String) {
        self.issuer = issuer; self.subject = subject; self.sessionID = sessionID
    }
    public var accountNamespace: String { Self.digest([issuer, subject]) }
    public var sessionFingerprint: String { Self.digest([issuer, subject, sessionID]) }
    private static func digest(_ fields: [String]) -> String {
        SHA256.hash(data: try! JSONEncoder().encode(fields)).map { String(format: "%02x", $0) }.joined()
    }
}
public struct AuthLease: Equatable, Sendable {
    public let identity: AuthIdentity
    public let generation: Int
}
@MainActor public final class AuthSessionFence {
    public private(set) var identity: AuthIdentity?
    public private(set) var generation = 0
    public var onLock: (() -> Void)?
    public init() {}
    public func activate(_ identity: AuthIdentity) -> AuthLease {
        generation += 1; self.identity = identity
        return AuthLease(identity: identity, generation: generation)
    }
    public func suspend() { generation += 1; identity = nil }
    public func lock() {
        generation += 1; identity = nil; onLock?()
    }
    public func validate(_ lease: AuthLease) throws {
        guard identity != nil else { throw AuthenticationFailure.locked }
        guard identity == lease.identity, generation == lease.generation else { throw AuthenticationFailure.changedSession }
    }
}
@MainActor public protocol SessionTokenProvider: AnyObject {
    var currentIdentity: AuthIdentity? { get }
    func token(for identity: AuthIdentity, forceRefresh: Bool) async throws -> String?
}

/// Strict admission uses exactly three string properties, rejecting even escaped duplicate keys.
public struct AuthAdmission: Equatable, Sendable {
    public let issuer: String
    public let subject: String
    public static func decode(_ data: Data) throws -> AuthAdmission {
        guard data.count <= 8192 else { throw AuthenticationFailure.profileMismatch }
        let bytes = Array(data); var offset = 0
        func space() { while offset < bytes.count && [9, 10, 13, 32].contains(bytes[offset]) { offset += 1 } }
        func take(_ byte: UInt8) throws { space(); guard offset < bytes.count, bytes[offset] == byte else { throw AuthenticationFailure.profileMismatch }; offset += 1 }
        func string() throws -> String {
            space(); let start = offset; try take(34)
            var escaped = false
            while offset < bytes.count {
                let byte = bytes[offset]; offset += 1
                if escaped { escaped = false; continue }
                if byte == 92 { escaped = true; continue }
                if byte == 34 {
                    do { return try JSONDecoder().decode(String.self, from: Data(bytes[start..<offset])) }
                    catch { throw AuthenticationFailure.profileMismatch }
                }
                if byte < 32 { throw AuthenticationFailure.profileMismatch }
            }
            throw AuthenticationFailure.profileMismatch
        }
        try take(123); var values: [String: String] = [:]
        for index in 0..<3 {
            if index > 0 { try take(44) }
            let key = try string(); guard values[key] == nil else { throw AuthenticationFailure.profileMismatch }
            try take(58); values[key] = try string()
        }
        try take(125); space()
        guard offset == bytes.count, Set(values.keys) == ["schema_version", "issuer", "subject"], values["schema_version"] == "1",
              let issuer = values["issuer"], !issuer.isEmpty, let subject = values["subject"], !subject.isEmpty else { throw AuthenticationFailure.profileMismatch }
        return AuthAdmission(issuer: issuer, subject: subject)
    }
}

/// Captures one endpoint/session for its lifetime; admission never survives a generation change.
@MainActor public final class AuthenticatedTransport: HTTPTransport {
    private let base: any HTTPTransport
    private let provider: any SessionTokenProvider
    private let fence: AuthSessionFence
    private let lease: AuthLease
    private let origin: URL
    private var refreshing: (UUID, Task<String?, Error>)?
    private var admissionGeneration = 0
    private var admittedGeneration: Int?
    public var onAdmissionInvalidated: (() -> Void)?
    public var onAdmissionRestored: (() -> Void)?
    public var isAdmitted: Bool { admittedGeneration == admissionGeneration && admittedGeneration != nil && (try? validateContext()) != nil }
    public init(base: any HTTPTransport, provider: any SessionTokenProvider, fence: AuthSessionFence, lease: AuthLease, origin: URL) throws {
        guard let validated = try? APIOrigin.url(origin.absoluteString),
              validated.host != URL(string: lease.identity.issuer)?.host?.lowercased() else { throw AuthenticationFailure.invalidDestination }
        self.base = base; self.provider = provider; self.fence = fence; self.lease = lease; self.origin = validated
    }
    private func validateContext() throws {
        try Task.checkCancellation(); try fence.validate(lease)
        guard provider.currentIdentity == lease.identity else { throw AuthenticationFailure.changedSession }
    }
    public func validate() throws {
        try validateContext()
        guard admittedGeneration == admissionGeneration, admittedGeneration != nil else { throw AuthenticationFailure.notAdmitted }
    }
    private func validateGeneration(_ generation: Int) throws {
        try validateContext()
        guard generation == admissionGeneration else { throw AuthenticationFailure.changedSession }
    }
    public func invalidateAdmission() {
        admissionGeneration += 1; admittedGeneration = nil; onAdmissionInvalidated?()
    }
    public func cancel() { invalidateAdmission(); refreshing?.1.cancel(); refreshing = nil }
    private func getToken(force: Bool) async throws -> String {
        try validateContext()
        let token: String?
        do {
            if force {
                if let task = refreshing { token = try await task.1.value }
                else {
                    let id = UUID()
                    let task = Task { try await provider.token(for: lease.identity, forceRefresh: true) }
                    refreshing = (id, task)
                    defer { if refreshing?.0 == id { refreshing = nil } }
                    token = try await task.value
                }
            } else { token = try await provider.token(for: lease.identity, forceRefresh: false) }
        } catch {
            try validateContext()
            if error is URLError { throw AuthenticationFailure.unavailable }
            fence.lock(); throw error
        }
        try validateContext()
        guard let token, !token.isEmpty, token.utf8.count <= 16_384,
              token.unicodeScalars.allSatisfy({ $0.value > 32 && $0.value < 127 }) else {
            fence.lock(); throw AuthenticationFailure.invalidToken
        }
        return token
    }
    private func authorized(_ original: URLRequest, token: String) -> URLRequest {
        var request = original
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        return request
    }
    /// Call before creating/opening any account storage. No domain endpoint is probed on failure.
    public func admit() async throws {
        try validateContext(); invalidateAdmission()
        let generation = admissionGeneration
        do {
            var forced = false
            try await handshake(generation: generation, forceInitially: false, forced: &forced)
        } catch {
            if generation == admissionGeneration { admittedGeneration = nil }
            throw error
        }
    }
    private func handshake(generation: Int, forceInitially: Bool, forced: inout Bool) async throws {
        var force = forceInitially
        while true {
            try validateGeneration(generation)
            let token = try await getToken(force: force)
            if force { forced = true }
            try validateGeneration(generation)
            var request = URLRequest(url: origin.appendingPathComponent("auth/v1/session"))
            request.httpMethod = "GET"; request.setValue("application/json", forHTTPHeaderField: "Accept")
            let response = try await base.send(authorized(request, token: token))
            try validateGeneration(generation)
            if response.status == 401 {
                if !forced { force = true; continue }
                fence.lock(); throw AuthenticationFailure.unauthorized
            }
            if response.status == 403 { fence.lock(); throw AuthenticationFailure.pendingSession }
            if response.status == 503 { throw AuthenticationFailure.unavailable }
            guard response.status == 200,
                  response.contentType?.split(separator: ";").first?.trimmingCharacters(in: .whitespaces).lowercased() == "application/json" else { throw AuthenticationFailure.profileMismatch }
            let admission = try AuthAdmission.decode(response.body)
            guard admission.issuer == lease.identity.issuer, admission.subject == lease.identity.subject else { throw AuthenticationFailure.profileMismatch }
            try validateGeneration(generation); admittedGeneration = generation; onAdmissionRestored?(); return
        }
    }
    public func send(_ original: URLRequest) async throws -> HTTPResult {
        try validate()
        guard let url = original.url, url.scheme == origin.scheme, url.host == origin.host, url.port == origin.port,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path != "/demo", url.path != "/auth/v1/session" else { throw AuthenticationFailure.invalidDestination }
        var generation = admissionGeneration
        var forced = false
        for attempt in 0...1 {
            try validateGeneration(generation)
            let token = try await getToken(force: false)
            try validateGeneration(generation)
            let response = try await base.send(authorized(original, token: token))
            try validateGeneration(generation)
            switch response.status {
            case 401:
                invalidateAdmission()
                if attempt == 1 || forced { fence.lock(); throw AuthenticationFailure.unauthorized }
                generation = admissionGeneration
                // The refresh budget belongs to this logical data send AND its re-admission.
                try await handshake(generation: generation, forceInitially: true, forced: &forced)
            case 403:
                invalidateAdmission(); fence.lock(); throw AuthenticationFailure.pendingSession
            case 503: throw AuthenticationFailure.unavailable
            case 300..<400:
                invalidateAdmission(); throw AuthenticationFailure.profileMismatch
            default: return response
            }
        }
        fence.lock(); throw AuthenticationFailure.unauthorized
    }
}

/// Only hashed account namespaces become paths. No JWT, email or raw subject is written.
public struct AccountCache: Sendable {
    public let root: URL
    private let deployment: String
    private var deploymentMarker: URL { root.appendingPathComponent("active-deployment") }
    private var marker: URL { root.appendingPathComponent("active-account") }
    public init(root: URL, deployment: String = "test-only") { self.root = root; self.deployment = SHA256.hash(data: Data(deployment.utf8)).map { String(format: "%02x", $0) }.joined() }
    private func secureDirectory() throws {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        var folder = root; var values = URLResourceValues(); values.isExcludedFromBackup = true
        try folder.setResourceValues(values)
        #if os(iOS)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: root.path)
        #endif
    }
    private func readNamespace(_ file: URL) throws -> String? {
        let data: Data
        do { data = try Data(contentsOf: file) }
        catch let error as NSError where error.domain == NSCocoaErrorDomain && error.code == NSFileReadNoSuchFileError { return nil }
        guard let value = String(data: data, encoding: .utf8),
              value.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else { throw AuthenticationFailure.locked }
        return value
    }
    public func directory(for identity: AuthIdentity) -> URL {
        root.appendingPathComponent(deployment, isDirectory: true).appendingPathComponent(identity.accountNamespace, isDirectory: true)
    }
    /// Old storage lacked deployment in its path. Move only with its recorded binding;
    /// never guess a deployment, merge queues, or discard a colliding destination.
    private func migrateLegacyActive() throws {
        guard let account = try readNamespace(marker) else { return }
        let old = root.appendingPathComponent(account, isDirectory: true)
        guard FileManager.default.fileExists(atPath: old.path) else { return }
        // Sanitize before inspecting binding/collision: even refused activation must not
        // leave a valid downloaded read cache behind. Pending bytes/metadata are preserved.
        _ = try ProtectedStorage(directory: old).load()
        guard let previous = try readNamespace(deploymentMarker) else { throw AuthenticationFailure.locked }
        let parent = root.appendingPathComponent(previous, isDirectory: true)
        let destination = parent.appendingPathComponent(account, isDirectory: true)
        if FileManager.default.fileExists(atPath: destination.path) {
            _ = try ProtectedStorage(directory: destination).load()
            throw AuthenticationFailure.locked
        }
        try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
        try FileManager.default.moveItem(at: old, to: destination)
    }
    public func activate(_ identity: AuthIdentity) throws -> ProtectedStorage {
        try secureDirectory()
        let unbound = root.appendingPathComponent(identity.accountNamespace, isDirectory: true)
        if FileManager.default.fileExists(atPath: unbound.path) {
            _ = try ProtectedStorage(directory: unbound).load()
        }
        try migrateLegacyActive()
        // An unbound legacy directory must be reviewed, never adopted by the current server.
        guard !FileManager.default.fileExists(atPath: unbound.path) else { throw AuthenticationFailure.locked }
        try Data(deployment.utf8).write(to: deploymentMarker, options: .atomic)
        var deploymentFile = deploymentMarker; var excluded = URLResourceValues(); excluded.isExcludedFromBackup = true; try deploymentFile.setResourceValues(excluded)
        #if os(iOS)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: deploymentMarker.path)
        #endif
        let data = Data(identity.accountNamespace.utf8)
        #if os(iOS)
        try data.write(to: marker, options: [.atomic, .completeFileProtection])
        #else
        try data.write(to: marker, options: .atomic)
        #endif
        var target = marker; var values = URLResourceValues(); values.isExcludedFromBackup = true; try target.setResourceValues(values)
        return ProtectedStorage(directory: directory(for: identity))
    }
    /// Detach the active account on lock/logout, retaining its isolated pending uploads.
    /// Auth fences destroy access immediately; only backend acknowledgement retires queued bytes.
    public func clearActive() throws {
        try migrateLegacyActive()
        if FileManager.default.fileExists(atPath: marker.path) { try FileManager.default.removeItem(at: marker) }
    }
}
