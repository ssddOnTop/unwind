import Foundation

/// Local optional answers, separate from immutable evidence and backend feedback.
/// The visit projection supplies opaque evidence-version keys; an epoch never migrates answers.
public struct VisitAnswerStore: Sendable {
    private struct Saved: Codable { let epoch: String; var answers: [String: VisitClarificationResponse] }
    public let directory: URL
    public init(directory: URL) { self.directory = directory }
    private var file: URL { directory.appendingPathComponent("visit-answers.json") }
    public func answers(epoch: String) throws -> [String: VisitClarificationResponse] {
        guard FileManager.default.fileExists(atPath: file.path) else { return [:] }
        let value = try JSONDecoder().decode(Saved.self, from: Data(contentsOf: file))
        return value.epoch == epoch ? value.answers : [:]
    }
    public func save(_ response: VisitClarificationResponse, epoch: String) throws {
        var current = try answers(epoch: epoch)
        if current.count >= 128 && current[response.key] == nil { current.removeAll() }
        current[response.key] = response
        try write(Saved(epoch: epoch, answers: current))
    }
    public func clear(epoch: String) throws { try write(Saved(epoch: epoch, answers: [:])) }
    private func write(_ value: Saved) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var folder = directory, target = file
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try folder.setResourceValues(values)
        #if os(iOS)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: directory.path)
        try JSONEncoder().encode(value).write(to: target, options: [.atomic, .completeFileProtection])
        #else
        try JSONEncoder().encode(value).write(to: target, options: .atomic)
        #endif
        try target.setResourceValues(values)
    }
}
