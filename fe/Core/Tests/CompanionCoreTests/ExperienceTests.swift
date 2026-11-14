import Foundation
import Testing
@testable import CompanionCore

private func experience() throws -> ExperienceV1 {
    try Wire.decode(ExperienceV1.self, Data(contentsOf: Bundle.module.url(forResource: "experience.example", withExtension: "json", subdirectory: "Fixtures")!))
}
private func answer(_ e: ExperienceV1) -> ExperienceAnswerRequest {
    ExperienceAnswerRequest(schema_version: "1", request_id: "answer-stable", data_epoch: e.data_epoch, expected_revision: e.revision, clarification_key: "opaque-from-response", answer: "not_sure")
}
@MainActor private final class ExperienceHTTP: HTTPTransport {
    var state: State
    var value: ExperienceV1
    var requests: [URLRequest] = []
    var failures = 0
    var rejection: String?
    var oldReceipt: ExperienceV1?
    init() throws {
        state = try fixture(); value = try experience(); state.data_epoch = value.data_epoch; state.revision = value.revision
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        if request.url?.path == "/state" { return HTTPResult(status: 200, body: try Wire.encode(state)) }
        if request.url?.path == "/experience/v1" { return HTTPResult(status: 200, body: try Wire.encode(value)) }
        if failures > 0 { failures -= 1; throw URLError(.networkConnectionLost) }
        if let rejection {
            let envelope = APIErrorEnvelope(error: APIErrorEnvelopeError(code: rejection, message: "Refresh the current question", retryable: false, request_id: "answer-stable", details: []))
            return HTTPResult(status: 409, body: try Wire.encode(envelope))
        }
        state.revision += 1; value.revision = state.revision
        value.home.headline = "Authoritative backend response"
        return HTTPResult(status: 200, body: try Wire.encode(oldReceipt ?? value))
    }
}
@Test func experienceExactSchemaExampleAndExplicitNullsRoundTrip() throws {
    let source = try Data(contentsOf: Bundle.module.url(forResource: "experience.example", withExtension: "json", subdirectory: "Fixtures")!)
    let encoded = try dictionary(experience())
    #expect(try encoded as NSDictionary == JSONSerialization.jsonObject(with: source) as! NSDictionary)
    #expect(encoded["clarification"] is NSNull)
    let home = encoded["home"] as! [String: Any]
    #expect(home["sample_label"] is NSNull)
    let action = home["primary_action"] as! [String: Any]
    #expect(action["detail"] is NSNull); #expect(action["card_id"] is NSNull); #expect(action["suggestion_id"] is NSNull)
}
@Test func experienceRejectsMissingNullUnknownFieldsAndUnknownAction() throws {
    var object = try dictionary(experience()); object.removeValue(forKey: "clarification")
    #expect(throws: (any Error).self) { try Wire.decode(ExperienceV1.self, JSONSerialization.data(withJSONObject: object)) }
    object = try dictionary(experience()); object["localInference"] = "forbidden"
    #expect(throws: (any Error).self) { try Wire.decode(ExperienceV1.self, JSONSerialization.data(withJSONObject: object)) }
    var value = try experience(); value.home.primary_action?.kind = "execute_url"
    #expect(throws: (any Error).self) { try Wire.decode(ExperienceV1.self, Wire.encode(value)) }
}
@Test func answerContainsOnlyExactContractFieldsNotLocalIdentityOrCopy() throws {
    let value = try answer(experience()); let encoded = try dictionary(value)
    #expect(Set(encoded.keys) == ["schema_version", "request_id", "data_epoch", "expected_revision", "clarification_key", "answer"])
    #expect(try Wire.decode(ExperienceAnswerRequest.self, Wire.encode(value)) == value)
    var bad = value; bad.answer = "confirmed"; #expect(throws: (any Error).self) { try Wire.decode(ExperienceAnswerRequest.self, Wire.encode(bad)) }
}
@Test @MainActor func experienceTransportUsesBackendCopyAndDropsLowerRevision() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try ExperienceHTTP(); let client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh(); let first = try await client.refreshExperience()
    #expect(first == http.value)
    http.value.revision = 0; http.value.home.headline = "Old copy must not win"
    #expect(try await client.refreshExperience() == first)
}
@Test @MainActor func oldExperienceCannotOverrideNewerStateRevision() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try ExperienceHTTP(); let client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh(); _ = try await client.refreshExperience()
    http.state.revision = 100; try await client.refresh(); http.value.revision = 0
    await #expect(throws: ClientFailure.rejected("stale_experience", false)) { try await client.refreshExperience() }
}
@Test @MainActor func answerRetriesExactEpochKeyRevisionAndRequestIdentity() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try ExperienceHTTP(); http.failures = 1
    let client = try CompanionClient(storage: storage, transport: http, retryDelay: 0)
    try await client.refresh(); let shown = try await client.refreshExperience()
    let outgoing = try PendingMutation(path: "/experience/v1/answers", request: answer(shown))
    try client.enqueue(outgoing); try await client.synchronize()
    let sent = http.requests.filter { $0.url?.path == "/experience/v1/answers" }
    #expect(sent.count == 2); #expect(sent.allSatisfy { $0.httpBody == outgoing.body })
    #expect(client.snapshot.outbox.isEmpty)
    #expect(client.experienceSnapshot == http.value, "The POST response is retained directly after fresh state validation")
    #expect(try await client.refreshExperience() == http.value)
    #expect(client.experienceSnapshot?.home.headline == "Authoritative backend response")
}
@Test @MainActor func staleOrObsoleteAnswersAreNotRetaggedOrAutomaticallyResent() async throws {
    for code in ["stale_experience", "obsolete_pattern"] {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let http = try ExperienceHTTP(); http.rejection = code
        let client = try CompanionClient(storage: storage, transport: http, retryDelay: 0)
        try await client.refresh(); let shown = try await client.refreshExperience()
        let pending = try PendingMutation(path: "/experience/v1/answers", request: answer(shown))
        try client.enqueue(pending)
        await #expect(throws: ClientFailure.rejected(code, false)) { try await client.synchronize() }
        #expect(client.snapshot.outbox.first?.body == pending.body)
        #expect(client.snapshot.outbox.first?.blockedCode == code)
        try client.discardObsoleteAnswers(); try await client.refresh(); _ = try await client.refreshExperience()
        #expect(client.snapshot.outbox.isEmpty)
        #expect(http.requests.filter { $0.url?.path == "/experience/v1/answers" }.count == 1)
    }
}
@Test @MainActor func epochResetClearsExperienceAndQueuedAnswers() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try ExperienceHTTP(); let client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh(); let shown = try await client.refreshExperience()
    try client.enqueue(PendingMutation(path: "/experience/v1/answers", request: answer(shown)))
    http.state.data_epoch = "new-epoch"; try await client.refresh()
    #expect(client.experienceSnapshot == nil); #expect(client.snapshot.outbox.isEmpty)
    await #expect(throws: ClientFailure.rejected("stale_experience", false)) { try await client.refreshExperience() }
}
@Test func experienceActualEncodedCasesForSchemaValidation() throws {
    let e = try experience()
    let cases: [[String: Any]] = [["definition": "experience", "payload": try dictionary(e)], ["definition": "answer_request", "payload": try dictionary(answer(e))]]
    let directory = URL(fileURLWithPath: ProcessInfo.processInfo.environment["COMPANION_EVIDENCE_DIR"]!)
    try JSONSerialization.data(withJSONObject: cases, options: [.prettyPrinted, .sortedKeys]).write(to: directory.appendingPathComponent("encoded-experience-contracts.json"))
    #expect(cases.count == 2)
}

@Test @MainActor func oldAnswerReceiptNeverRestoresPreviousQuestion() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try ExperienceHTTP(); let client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh(); let previous = try await client.refreshExperience(); http.oldReceipt = previous
    try client.enqueue(PendingMutation(path: "/experience/v1/answers", request: answer(previous)))
    try await client.synchronize()
    #expect(client.snapshot.outbox.isEmpty)
    #expect(client.experienceSnapshot == nil)
    #expect(try await client.refreshExperience() == http.value)
}
