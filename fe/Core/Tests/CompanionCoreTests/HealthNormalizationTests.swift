import Foundation
import Testing
@testable import CompanionCore

private func healthRecord(_ id: String = "sample-one") -> NormalizedHealthSample {
    NormalizedHealthSample(sample_id: id, hk_type: "stepCount", kind: "dailyCumulative", start_at: "2026-11-12T00:00:00Z", end_at: "2026-11-13T00:00:00Z", timezone: "UTC", value: 9000, unit: "count", category: nil, workout_activity: nil, duration_seconds: nil, energy_kcal: nil, distance_m: nil, source_name: "Synthetic HealthKit helper", revision: 1, provenance: "synthetic", source_bundle: HealthNormalization.helperBundle, source_revision: "1", dataset_id: HealthNormalization.dataset, recipe: HealthNormalization.recipe)
}

@Test func healthProvenanceRequiresCanonicalBundleBooleanDatasetAndRecipe() {
    let b = HealthNormalization.helperBundle, r = HealthNormalization.recipe
    #expect(r == "six-month-pattern-demo-v1:1")
    #expect(HealthNormalization.syntheticFlag(true))
    #expect(HealthNormalization.syntheticFlag(NSNumber(value: 1)))
    #expect(!HealthNormalization.syntheticFlag(NSNumber(value: 2)))
    #expect(!HealthNormalization.syntheticFlag("true"))
    #expect(HealthNormalization.provenance(bundle: b, synthetic: true, dataset: HealthNormalization.dataset, recipe: HealthNormalization.dataset) == nil)
    #expect(HealthNormalization.provenance(bundle: b, synthetic: true, dataset: HealthNormalization.dataset, recipe: r) == "synthetic")
    #expect(HealthNormalization.provenance(bundle: b, synthetic: false, dataset: HealthNormalization.dataset, recipe: r) == nil)
    #expect(HealthNormalization.provenance(bundle: b, synthetic: true, dataset: "org.localcompanion.test.healthseeder.probe.v1.2026-11-14", recipe: "bounded-probe-v1") == nil)
    #expect(HealthNormalization.provenance(bundle: "synthetic-looking-name", synthetic: false, dataset: nil, recipe: nil) == "realHealthKit")
    #expect(HealthNormalization.provenance(bundle: "another.bundle", synthetic: true, dataset: HealthNormalization.dataset, recipe: r) == nil)
}

@Test func healthBatchesBoundRecordsPreserveEpochAndRequireCompleteExplicitKeys() throws {
    let f = ISO8601DateFormatter(), start = f.date(from: "2026-05-14T00:00:00Z")!, end = f.date(from: "2026-11-14T00:00:00Z")!
    let epoch = UUID().uuidString, collection = UUID().uuidString
    let pages = try HealthNormalization.batches(samples: (0..<205).map { healthRecord("sample-\($0)") }, epoch: epoch, start: start, end: end, collectionID: collection)
    #expect(pages.map { $0.samples.count } == [100,100,5])
    #expect(pages.map(\.page_index) == [0,1,2])
    #expect(pages.allSatisfy { $0.page_count == 3 && $0.data_epoch == epoch && $0.collection_id == collection })
    #expect(Set(pages.map(\.request_id)).count == 3)
    for page in pages {
        #expect(try Wire.decode(HealthIngestRequest.self, Wire.encode(page)) == page)
        let pending = try PendingMutation(path: "/health/v1/samples", request: page)
        #expect(pending.epoch == epoch && pending.id == page.request_id)
        #expect(try Wire.decode(HealthIngestRequest.self, pending.body) == page)
    }
    let empty = try HealthNormalization.batches(samples: [], epoch: epoch, start: start, end: end)
    #expect(empty.count == 1 && empty[0].samples.isEmpty)
    #expect(throws: (any Error).self) { try HealthNormalization.batches(samples: [healthRecord(),healthRecord()], epoch: epoch, start: start, end: end) }
    var wire = try dictionary(pages[0]);wire["account_id"] = "foreign"
    #expect(throws: (any Error).self) { try Wire.decode(HealthIngestRequest.self, JSONSerialization.data(withJSONObject: wire)) }
}

@MainActor private final class HealthHTTP: HTTPTransport {
    var state: State
    var requests: [URLRequest] = []
    var fail = true
    init() throws { state = try fixture() }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        if request.url?.path == "/health/v1/samples", fail { throw URLError(.networkConnectionLost) }
        return HTTPResult(status: 200, body: try Wire.encode(state))
    }
}

@Test @MainActor func healthOutboxSurvivesOfflineWithStableIDsAndErasureFencesOldEpoch() async throws {
    let storage = temporaryStorage();defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try HealthHTTP(), client = try CompanionClient(storage: storage, transport: http, retryDelay: 0)
    try await client.refresh()
    let f = ISO8601DateFormatter()
    let page = try HealthNormalization.batches(samples: [healthRecord()], epoch: http.state.data_epoch, start: f.date(from: "2026-05-14T00:00:00Z")!, end: f.date(from: "2026-11-14T00:00:00Z")!)[0]
    try client.enqueue(PendingMutation(path: "/health/v1/samples", request: page))
    await #expect(throws: (any Error).self) { try await client.synchronize() }
    let sent = http.requests.filter { $0.url?.path == "/health/v1/samples" }
    #expect(sent.count == 3 && sent.allSatisfy { $0.httpBody == sent[0].httpBody })
    #expect(try storage.load().outbox.count == 1)
    http.fail = false
    try await client.synchronize()
    #expect(client.snapshot.outbox.isEmpty)
    try client.enqueue(PendingMutation(path: "/health/v1/samples", request: page))
    http.state.data_epoch = UUID().uuidString
    let count = http.requests.filter { $0.url?.path == "/health/v1/samples" }.count
    try await client.synchronize()
    #expect(client.snapshot.outbox.isEmpty && client.snapshot.quarantinedUploadCount == 1)
    #expect(client.snapshot.state == http.state)
    #expect(try storage.load().quarantinedUploads == client.snapshot.quarantinedUploads)
    #expect(http.requests.filter { $0.url?.path == "/health/v1/samples" }.count == count)
}
