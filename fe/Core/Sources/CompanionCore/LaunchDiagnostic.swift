import Foundation
import OSLog

/// Local-only launch diagnostics. No error descriptions, payloads, identifiers or keys.
/// The revision identifies this diagnostic format, not the installed app build.
public enum LaunchDiagnostic {
    public enum Stage: String, Sendable {
        case synchronize, stateRequest, stateDecode, persistence
        case experienceRequest, experienceDecode, otherRequest, reminders
    }
    public enum Category: String, Sendable {
        case busy, rejected, pendingChanges, invalidResponse, clientOther
        case authentication, transport, decoding, encoding, cancelled, storage, unknown
        case httpClientError, httpServerError, httpOther
    }
    public struct Event: Equatable, Sendable {
        public let stage: Stage
        public let category: Category
        public var line: String { "launch-v1 stage=\(stage.rawValue) category=\(category.rawValue)" }
    }
    @TaskLocal public static var enabled = false
    @TaskLocal static var sink: (@Sendable (Event) -> Void)?
    private static let logger = Logger(subsystem: "CompanionCore", category: "LaunchDiagnostic")

    public static func category(_ error: any Error) -> Category {
        if let failure = error as? ClientFailure {
            switch failure {
            case .busy: return .busy
            case .rejected: return .rejected // Never include the server-provided code.
            case .pendingChanges: return .pendingChanges
            case .invalidResponse: return .invalidResponse
            default: return .clientOther
            }
        }
        if error is AuthenticationFailure { return .authentication }
        if error is CancellationError { return .cancelled }
        if error is URLError { return .transport }
        if error is DecodingError { return .decoding }
        if error is EncodingError { return .encoding }
        if (error as NSError).domain == NSCocoaErrorDomain { return .storage }
        return .unknown
    }
    public static func failure(_ error: any Error, at stage: Stage) {
        emit(Event(stage: stage, category: category(error)))
    }
    static func http(_ status: Int, at stage: Stage) {
        let category: Category = (400..<500).contains(status) ? .httpClientError
            : (500..<600).contains(status) ? .httpServerError : .httpOther
        emit(Event(stage: stage, category: category))
    }
    private static func emit(_ event: Event) {
        guard enabled else { return }
        if let sink { sink(event) }
        else { logger.error("\(event.line, privacy: .public)") }
    }
    static func decode<T: Decodable>(_ type: T.Type, _ data: Data, at stage: Stage) throws -> T {
        do { return try Wire.decode(type, data) }
        catch { failure(error, at: stage); throw error }
    }
}
