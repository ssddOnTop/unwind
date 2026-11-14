// TEST ONLY. Standalone simulator app; never include in the product target.
// Full six-month generation is gated on an explicit approved recipe digest.
import SwiftUI
import HealthKit

@main
struct SyntheticHealthDemo: App {
    @StateObject private var probe = Probe()
    @StateObject private var full = FullSeed()
    var body: some Scene {
        WindowGroup {
            VStack(spacing: 20) {
                Text("Synthetic Health Demo").font(.title)
                Text("TEST ONLY · synthetic samples · no real provenance")
                Text(full.status).accessibilityIdentifier("fullSeedStatus")
                Text(probe.status).accessibilityIdentifier("probeStatus")
                Button("Authorize and run bounded probe") { Task { await probe.run() } }
                    .disabled(probe.busy || full.busy)
                Button("Remove only this helper's probe") { Task { await probe.cleanup() } }
                    .disabled(probe.busy || full.busy)
                Text("Full seeding requires a canonical recipe and its approved SHA-256 launch argument.")
            }.padding().task {
                let arguments = ProcessInfo.processInfo.arguments
                if let index = arguments.firstIndex(of: "--seed-sha256"), index + 1 < arguments.count {
                    await full.run(approvedDigest: arguments[index + 1])
                } else if arguments.contains("--probe") { await probe.run() }
            }
        }
    }
}

@MainActor
final class Probe: ObservableObject {
    @Published var status = "Ready; no samples written"
    @Published var busy = false
    private let store = HKHealthStore()
    private let batch = "org.localcompanion.test.healthseeder.probe.v1.2026-11-14"
    private let prefix = "org.localcompanion.test.healthseeder."
    private let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    private var types: [HKSampleType] {
        [HKQuantityType.quantityType(forIdentifier: .stepCount)!,
         HKCategoryType.categoryType(forIdentifier: .sleepAnalysis)!,
         HKQuantityType.quantityType(forIdentifier: .heartRate)!,
         HKQuantityType.quantityType(forIdentifier: .restingHeartRate)!,
         HKObjectType.workoutType()]
    }
    private func report(_ message: String, rows: [[String: String]] = []) {
        status = message
        let object: [String: Any] = ["status": message, "datasetID": batch,
            "sourceBundle": HKSource.default().bundleIdentifier, "rows": rows,
            "note": "Own-sample readback does not prove permission to read other sources"]
        do {
            let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: directory.appendingPathComponent("probe-result.json"), options: .atomic)
        } catch { status += " Evidence write failed: \(error)" }
    }
    private func metadata(_ key: String) -> [String: Any] {
        [prefix + "synthetic": true, prefix + "datasetID": batch,
         prefix + "recipe": "bounded-probe-v1", prefix + "timezone": "UTC",
         prefix + "provenance": "synthetic-test-only-not-person-data",
         HKMetadataKeySyncIdentifier: batch + "." + key,
         HKMetadataKeySyncVersion: NSNumber(value: 1)]
    }
    private func samples() -> [HKSample] {
        let day = ISO8601DateFormatter().date(from: "2026-11-13T00:00:00Z")!
        func time(_ hours: Double) -> Date { day.addingTimeInterval(hours * 3600) }
        return [
            HKQuantitySample(type: types[0] as! HKQuantityType, quantity: HKQuantity(unit: .count(), doubleValue: 120), start: time(12), end: time(12.1), metadata: metadata("steps")),
            HKCategorySample(type: types[1] as! HKCategoryType, value: HKCategoryValueSleepAnalysis.asleepCore.rawValue, start: time(1), end: time(1.5), metadata: metadata("sleep")),
            HKQuantitySample(type: types[2] as! HKQuantityType, quantity: HKQuantity(unit: .count().unitDivided(by: .minute()), doubleValue: 72), start: time(12), end: time(12), metadata: metadata("heart")),
            HKQuantitySample(type: types[3] as! HKQuantityType, quantity: HKQuantity(unit: .count().unitDivided(by: .minute()), doubleValue: 60), start: time(8), end: time(8), metadata: metadata("resting")),
            HKWorkout(activityType: .walking, start: time(13), end: time(13.1), duration: 360, totalEnergyBurned: nil, totalDistance: nil, metadata: metadata("workout"))
        ]
    }
    // Queries are restricted to our source AND dataset. Never enumerate personal data.
    private func owned(_ type: HKSampleType) async throws -> [HKSample] {
        let predicate = NSCompoundPredicate(andPredicateWithSubpredicates: [
            HKQuery.predicateForObjects(from: HKSource.default()),
            HKQuery.predicateForObjects(withMetadataKey: prefix + "datasetID", allowedValues: [batch])])
        return try await withCheckedThrowingContinuation { continuation in
            store.execute(HKSampleQuery(sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: samples ?? []) }
            })
        }
    }
    private func details(_ sample: HKSample, count: Int, result: String) -> [String: String] {
        var row = ["type": sample.sampleType.identifier, "uuid": sample.uuid.uuidString,
                   "source": sample.sourceRevision.source.bundleIdentifier, "result": result,
                   "ownedTypeCount": String(count),
                   "start": ISO8601DateFormatter().string(from: sample.startDate),
                   "end": ISO8601DateFormatter().string(from: sample.endDate)]
        if let quantity = sample as? HKQuantitySample {
            let unit: HKUnit = sample.sampleType == types[0] ? .count() : .count().unitDivided(by: .minute())
            row["value"] = String(quantity.quantity.doubleValue(for: unit))
            row["unit"] = unit.unitString
        }
        if let category = sample as? HKCategorySample { row["categoryValue"] = String(category.value) }
        if let workout = sample as? HKWorkout { row["durationSeconds"] = String(workout.duration) }
        if let metadata = sample.metadata,
           let data = try? JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys]) {
            row["metadataJSON"] = String(data: data, encoding: .utf8)
        }
        return row
    }
    private func manifest(_ samples: [HKSample]) throws {
        let url = directory.appendingPathComponent("owned-uuid-manifest.json")
        var records = [[String: String]]()
        if FileManager.default.fileExists(atPath: url.path) {
            records = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [[String: String]]
        }
        for sample in samples where !records.contains(where: { $0["uuid"] == sample.uuid.uuidString }) {
            records.append(["uuid": sample.uuid.uuidString, "type": sample.sampleType.identifier, "datasetID": batch])
        }
        try JSONSerialization.data(withJSONObject: records, options: [.prettyPrinted, .sortedKeys])
            .write(to: url, options: .atomic)
    }
    func run() async {
        guard !busy else { return }; busy = true; defer { busy = false }
        guard HKHealthStore.isHealthDataAvailable() else { report("HealthKit unavailable; DB fallback needed"); return }
        report("HealthKit available; waiting for human Health permissions")
        do {
            try await store.requestAuthorization(toShare: Set(types), read: Set(types.map { $0 as HKObjectType }))
            var rows = [[String: String]]()
            var allOwned = [HKSample]()
            for sample in samples() {
                let type = sample.sampleType
                guard store.authorizationStatus(for: type) == .sharingAuthorized else {
                    rows.append(["type": type.identifier, "result": "write not authorized; skipped"]); continue
                }
                let existing = try await owned(type)
                let sync = sample.metadata![HKMetadataKeySyncIdentifier] as! String
                if let saved = existing.first(where: { $0.metadata?[HKMetadataKeySyncIdentifier] as? String == sync }) {
                    allOwned.append(saved)
                    try manifest(allOwned)
                    rows.append(details(saved, count: existing.count, result: "existing exact sync sample; no write"))
                    continue
                }
                // Persist system-assigned UUID before save, so interruption remains recoverable.
                allOwned.append(sample); try manifest(allOwned)
                do {
                    try await store.save(sample)
                    let verified = try await owned(type)
                    guard let saved = verified.first(where: { $0.uuid == sample.uuid && $0.metadata?[HKMetadataKeySyncIdentifier] as? String == sync }) else {
                        try await store.delete(sample)
                        rows.append(["type": type.identifier, "result": "exact readback failed; removed newly created sample"])
                        continue
                    }
                    rows.append(details(saved, count: verified.count, result: "saved and exact UUID/source readback verified"))
                } catch {
                    // Delete only this newly constructed sample, never pre-existing data.
                    do { try await store.delete(sample) }
                    catch { rows.append(["type": type.identifier, "result": "cleanup not confirmed: \(error)"]) }
                    rows.append(["type": type.identifier, "result": "save/readback failed: \(error)"])
                }
            }
            report("Bounded probe finished; inspect per-type results", rows: rows)
        } catch { report("Authorization/query failed: \(error). DB fallback needed; no full injection.") }
    }
    func cleanup() async {
        guard !busy else { return }; busy = true; defer { busy = false }
        do {
            let data = try Data(contentsOf: directory.appendingPathComponent("owned-uuid-manifest.json"))
            let rows = try JSONSerialization.jsonObject(with: data) as! [[String: String]]
            let ids = Set(rows.compactMap { $0["uuid"] })
            var removed = 0
            for type in types {
                let matches = try await owned(type).filter { ids.contains($0.uuid.uuidString) }
                if !matches.isEmpty { try await store.delete(matches); removed += matches.count }
            }
            report("Removed \(removed) manifest-owned probe samples only")
        } catch { report("Scoped cleanup failed: \(error)") }
    }
}
