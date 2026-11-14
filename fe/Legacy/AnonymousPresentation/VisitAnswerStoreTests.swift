import Foundation
import Testing
@testable import CompanionCore

@Test func visitAnswersPersistLocallyAndNeverMigrateEpochs() throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let store = VisitAnswerStore(directory: storage.directory)
    let answer = VisitClarificationResponse(key: "opaque-pattern-key", answer: .no)
    try store.save(answer, epoch: "first")
    #expect(try VisitAnswerStore(directory: storage.directory).answers(epoch: "first")[answer.key] == answer)
    #expect(try store.answers(epoch: "second").isEmpty)
    try store.clear(epoch: "second")
    #expect(try store.answers(epoch: "first").isEmpty)
    #expect(try store.answers(epoch: "second").isEmpty)
    #expect(try storage.directory.appendingPathComponent("visit-answers.json").resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup == true)
}
