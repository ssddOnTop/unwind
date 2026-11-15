import Foundation
import OSLog

/// Explicit simulator launch mode. A preflight never constructs a domain client.
@MainActor public final class QueueCountPreflight {
    public static var requested: Bool {
        #if targetEnvironment(simulator)
        return ProcessInfo.processInfo.arguments.contains("--queue-count-preflight")
        #else
        return false
        #endif
    }
    public struct Counts: Equatable, Sendable {
        public let general: Int
        public let health: Int
        public let notificationRevocations: Int
        public init(snapshot: LocalSnapshot, notificationRevocations: Int) {
            health = snapshot.outbox.filter { $0.path == "/health/v1/incremental" }.count
            general = snapshot.outbox.count - health
            self.notificationRevocations = notificationRevocations
        }
        public var line: String {
            "queue-preflight-v1 status=available general=\(general) health=\(health) notification_revocations=\(notificationRevocations)"
        }
    }
    public private(set) var attempted = false
    public private(set) var result: String?
    private var stopped = false
    private let emit: (String) -> Void
    private static let logger = Logger(subsystem: "CompanionCore", category: "QueueCountPreflight")
    public init(emit: ((String) -> Void)? = nil) {
        self.emit = emit ?? { line in Self.logger.error("\(line, privacy: .public)") }
    }
    private func finish(_ line: String) {
        guard result == nil else { return }
        result = line; emit(line)
    }
    public func stop() {
        stopped = true; attempted = true
        finish("queue-preflight-v1 status=unavailable")
    }
    public func run(admit: () async throws -> Void, read: () throws -> Counts) async {
        guard !attempted, !stopped else { return }
        attempted = true
        do {
            try await admit()
            guard !stopped, !Task.isCancelled else { stop(); return }
            let counts = try read()
            guard !stopped, !Task.isCancelled else { stop(); return }
            finish(counts.line)
        } catch { finish("queue-preflight-v1 status=unavailable") }
    }
}
