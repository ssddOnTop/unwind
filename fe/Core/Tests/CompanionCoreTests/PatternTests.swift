import Foundation
import Testing
@testable import CompanionCore

private func patternFixture() throws -> PatternOverview {
    let json = #"{"schema_version":"1","status":"complete","run_id":"00000000-0000-4000-8000-000000000001","analyzed_at":"2026-11-14T20:00:00Z","engine":"LLMassisted","is_demo":true,"can_analyze":true,"source_label":"Operator demo — synthetic records","doctor_brief":"Synthetic patterns for review, not a diagnosis.","privacy_notice":"Only a pattern summary is sent.","bank_refresh":{"status":"not_requested","last_updated":null,"message":"Using saved bank data."},"evidence":{"sample_count_bucket":"1000plus","recent":{"label":"recent4weeks","steps_average_bucket":3000,"sleep_hours_bucket":6.0,"food_service_per_week_bucket":5,"workouts_per_week_bucket":1,"missing_steps":false,"missing_sleep":false,"missing_transactions":false},"baseline":{"label":"baseline","steps_average_bucket":9000,"sleep_hours_bucket":8.0,"food_service_per_week_bucket":1,"workouts_per_week_bucket":3,"missing_steps":false,"missing_sleep":false,"missing_transactions":false}},"findings":[{"concern_id":"sleep","title":"Shorter recorded sleep","explanation":"A recorded pattern, not a diagnosis.","evidence_codes":["sleep_comparison"],"confidence":"moderate","attribution":"unconfirmed","evidence_summary":"Recent 6 hours versus baseline 8 hours."}]}"#
    return try Wire.decode(PatternOverview.self, Data(json.utf8))
}

@Test func patternContractRejectsUnknownMissingAndUnapprovedConcern() throws {
    let value = try patternFixture()
    #expect(value.is_demo && value.evidence?.recent.sleep_hours_bucket == 6)
    #expect(try Wire.decode(PatternOverview.self, Wire.encode(value)) == value)
    var object = try dictionary(value)
    object["identity"] = "forbidden"
    #expect(throws: (any Error).self) { try Wire.decode(PatternOverview.self, JSONSerialization.data(withJSONObject: object)) }
    object = try dictionary(value); object.removeValue(forKey: "run_id")
    #expect(throws: (any Error).self) { try Wire.decode(PatternOverview.self, JSONSerialization.data(withJSONObject: object)) }
    var bad = value; bad.findings[0].concern_id = "disease_prediction"
    #expect(throws: (any Error).self) { try Wire.decode(PatternOverview.self, Wire.encode(bad)) }
    var empty = value; empty.evidence = nil; empty.analyzed_at = nil; empty.run_id = nil; empty.findings = []; empty.status = "not_analyzed"
    #expect(try Wire.decode(PatternOverview.self, Wire.encode(empty)) == empty)
}

@MainActor private final class PatternHTTP: HTTPTransport {
    var requests: [URLRequest] = []
    var fail = false
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        if request.url?.path == "/state" { return HTTPResult(status: 200, body: try Wire.encode(fixture())) }
        if fail { throw URLError(.networkConnectionLost) }
        if request.url?.path.hasPrefix("/workflows/v1") == true { return HTTPResult(status: 200, body: try Wire.encode(workflowFixture())) }
        return HTTPResult(status: 200, body: try Wire.encode(patternFixture()))
    }
}

@Test @MainActor func patternManualRequestIsMinimalNeverQueuedOrAutomaticallyRetried() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = PatternHTTP(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh()
    #expect(http.requests.count == 1, "Refreshing State must never start an LLM review")
    _ = try await client.patterns()
    #expect(http.requests.last?.httpMethod == "GET")
    let id = UUID().uuidString
    _ = try await client.patterns(requestID: id)
    let sent = try #require(http.requests.last)
    let body = try #require(sent.httpBody)
    let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    #expect(Set(object.keys) == ["schema_version", "request_id", "data_epoch", "fetch_latest_bank_data"])
    #expect(object["request_id"] as? String == id)
    #expect(object["fetch_latest_bank_data"] as? Bool == false)
    #expect(object["data_epoch"] as? String == client.snapshot.state?.data_epoch)
    #expect(client.snapshot.outbox.isEmpty)
    http.fail = true
    await #expect(throws: (any Error).self) { try await client.patterns(requestID: id) }
    #expect(http.requests.filter { $0.url?.path == "/patterns/v1/analyze" }.count == 2)
    #expect(http.requests.last?.httpBody == body)
    client.invalidateSession()
    let count = http.requests.count
    await #expect(throws: (any Error).self) { try await client.patterns(requestID: id) }
    #expect(http.requests.count == count)
}

@Test @MainActor func patternBankRefreshChoiceIsExplicitAndSessionScoped() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = PatternHTTP(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh()
    let model = AppModel(client: client, accountNamespace: "private-test")
    #expect(model.workflowOverview?.action == nil)
    _ = try await client.patterns(requestID: UUID().uuidString, fetchLatestBankData: true)
    let body = try #require(http.requests.last?.httpBody)
    let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
    #expect(object["fetch_latest_bank_data"] as? Bool == true)
    #expect(object["data_epoch"] as? String == client.snapshot.state?.data_epoch)
    var value = try patternFixture()
    value.bank_refresh.status = "partial"
    value.bank_refresh.message = "Refresh incomplete; analysis not run."
    #expect(try Wire.decode(PatternOverview.self, Wire.encode(value)).bank_refresh.status == "partial")
    value.bank_refresh.status = "pretend_fresh"
    #expect(throws: (any Error).self) { try Wire.decode(PatternOverview.self, Wire.encode(value)) }
    model.hideForAdmission()
    #expect(model.workflowOverview == nil)
}


private func workflowFixture() throws -> WorkflowOverview {
    WorkflowOverview(schema_version: "1", enabled: true, eligible: true, next_scheduled_at: "2026-11-15T00:00:00Z", consent_label: "Allow nightly review", consent_notice: "Coarse summaries only.", action: nil,
        pages: [WorkflowPage(id: "home", title: "Server-owned title", paragraphs: ["Nightly at 00:00 UTC."])],
        questions: [WorkflowQuestion(id: "attribution", label: "Server question", choices: [WorkflowChoice(id: "unsure", label: "Not sure")])],
        job: nil, report: try patternFixture())
}

@Test @MainActor func workflowQueueRequestIsSelfOnlyAndStrictWithoutClientModelOrFetchFlags() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = PatternHTTP(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh()
    let normal = try await client.workflows()
    #expect(normal.action == nil)
    #expect(normal.pages.first?.title == "Server-owned title")
    let id = UUID().uuidString
    _ = try await client.workflows(requestID: id)
    let first = try #require(http.requests.last?.httpBody)
    let body = try #require(JSONSerialization.jsonObject(with: first) as? [String: Any])
    #expect(Set(body.keys) == ["schema_version", "request_id", "data_epoch"])
    #expect(body["data_epoch"] as? String == client.snapshot.state?.data_epoch)
    _ = try await client.workflows(requestID: id)
    #expect(http.requests.last?.httpBody == first)
    #expect(client.snapshot.outbox.isEmpty)
    var invalid = try dictionary(normal)
    invalid["target_account"] = "forbidden"
    #expect(throws: (any Error).self) { try Wire.decode(WorkflowOverview.self, JSONSerialization.data(withJSONObject: invalid)) }
    invalid = try dictionary(normal); invalid["action"] = ["id":"execute_script", "label":"Forbidden"]
    #expect(throws: (any Error).self) { try Wire.decode(WorkflowOverview.self, JSONSerialization.data(withJSONObject: invalid)) }
    client.invalidateSession();let before = http.requests.count
    await #expect(throws: (any Error).self) { try await client.workflows(requestID: id) }
    #expect(http.requests.count == before)
}
