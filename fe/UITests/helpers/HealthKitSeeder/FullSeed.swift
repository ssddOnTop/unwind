// TEST ONLY. Standalone simulator helper. No network, DB, product imports or deletion.
// Input: own Documents/canonical-recipe.json plus explicit --seed-sha256 DIGEST.
import Foundation
import HealthKit
import CryptoKit
import SwiftUI

struct SeedRecipe: Decodable {
    let schema_version: Int
    let dataset_id: String
    let recipe_version: Int
    let source_commit: String
    let start: String
    let end: String
    let timezone: String
    let counts: [String: Int]
    let category_counts: [String: Int]
    let samples: [SeedRecord]
}

struct SeedRecord: Codable {
    let sample_id: String
    let hk_type: String
    let kind: String
    let start_at: String
    let end_at: String
    let timezone: String
    let value: Double?
    let unit: String?
    let category: String?
    let workout_activity: String?
    let duration_seconds: Double?
    let energy_kcal: Double?
    let distance_m: Double?
    let source_name: String
    let revision: Int
}

struct SeedFailure: LocalizedError {
    let reason: String
    var errorDescription: String? { reason }
}

@MainActor
final class FullSeed: ObservableObject {
    @Published var status = "Full seed requires an exported recipe and its approved digest"
    @Published var busy = false
    private let store = HKHealthStore()
    private let prefix = "org.localcompanion.test.healthseeder."
    private let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    private let types: [String: HKSampleType] = [
        "stepCount": HKQuantityType.quantityType(forIdentifier: .stepCount)!,
        "sleepAnalysis": HKCategoryType.categoryType(forIdentifier: .sleepAnalysis)!,
        "heartRate": HKQuantityType.quantityType(forIdentifier: .heartRate)!,
        "restingHeartRate": HKQuantityType.quantityType(forIdentifier: .restingHeartRate)!,
        "workout": HKObjectType.workoutType()
    ]
    private let stages: [String: HKCategoryValueSleepAnalysis] = [
        "inBed": .inBed, "asleepCore": .asleepCore, "asleepDeep": .asleepDeep,
        "asleepREM": .asleepREM, "awake": .awake
    ]
    private var manifest: [String: String] = [:]
    private var written = 0
    private var reused = 0
    private var digest = ""
    private var recipe: SeedRecipe?

    private func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw SeedFailure(reason: message) }
    }
    private func date(_ text: String) throws -> Date {
        guard let value = ISO8601DateFormatter().date(from: text) else {
            throw SeedFailure(reason: "Invalid UTC timestamp")
        }
        return value
    }
    private func output(_ phase: String, rows: [[String: Any]] = [], error: String? = nil) throws {
        status = phase
        var result: [String: Any] = ["phase": phase, "sha256": digest,
            "written": written, "reused": reused, "verified": rows.count,
            "rows": rows, "representationDifferences": [],
            "note": "Only this helper's dataset verified; not product collection or DB ingestion"]
        if let recipe {
            result["datasetID"] = recipe.dataset_id
            result["sourceCommit"] = recipe.source_commit
            result["expectedCounts"] = recipe.counts
            result["start"] = recipe.start; result["end"] = recipe.end
        }
        if let error { result["error"] = error }
        try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .prettyPrinted])
            .write(to: directory.appendingPathComponent("full-result.json"), options: .atomic)
    }
    private func checkpoint() throws {
        guard let recipe else { throw SeedFailure(reason: "Missing recipe") }
        let data: [String: Any] = ["datasetID": recipe.dataset_id, "sha256": digest,
                                  "sourceCommit": recipe.source_commit, "uuidsBySampleID": manifest]
        try JSONSerialization.data(withJSONObject: data, options: [.sortedKeys, .prettyPrinted])
            .write(to: directory.appendingPathComponent("full-owned-manifest.json"), options: .atomic)
    }
    private func metadata(_ record: SeedRecord, _ recipe: SeedRecipe) throws -> [String: Any] {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let normalized = String(decoding: try encoder.encode(record), as: UTF8.self)
        return [prefix + "synthetic": NSNumber(value: true),
                prefix + "datasetID": recipe.dataset_id,
                prefix + "recipe": recipe.dataset_id + ":" + String(recipe.recipe_version),
                prefix + "recipeSHA256": digest, prefix + "sourceCommit": recipe.source_commit,
                prefix + "timezone": "UTC", prefix + "provenance": "synthetic-test-only-not-person-data",
                prefix + "canonicalSampleID": record.sample_id,
                prefix + "normalizedRecord": normalized,
                HKMetadataKeySyncIdentifier: HKSource.default().bundleIdentifier + "." + recipe.dataset_id + "." + record.sample_id,
                HKMetadataKeySyncVersion: NSNumber(value: recipe.recipe_version)]
    }
    private func native(_ record: SeedRecord, _ recipe: SeedRecipe) throws -> HKSample {
        let a = try date(record.start_at), b = try date(record.end_at)
        let start = try date(recipe.start), end = try date(recipe.end)
        try require(start <= a && a < end && a <= b && b <= end, "Outside half-open dataset interval: " + record.sample_id)
        try require(record.timezone == "UTC" && record.revision == 1, "Unsupported timezone/revision")
        guard let type = types[record.hk_type] else { throw SeedFailure(reason: "Unsupported HealthKit type") }
        let md = try metadata(record, recipe)
        switch record.hk_type {
        case "stepCount", "heartRate", "restingHeartRate":
            guard let value = record.value, value.isFinite, value >= 0 else { throw SeedFailure(reason: "Invalid quantity") }
            let steps = record.hk_type == "stepCount"
            try require(record.unit == (steps ? "count" : "bpm"), "Wrong quantity unit")
            try require(record.kind == (steps ? "dailyCumulative" : "quantity"), "Wrong quantity kind")
            try require(steps ? b > a : b == a, "Wrong quantity interval")
            return HKQuantitySample(type: type as! HKQuantityType,
                quantity: HKQuantity(unit: steps ? .count() : .count().unitDivided(by: .minute()), doubleValue: value),
                start: a, end: b, metadata: md)
        case "sleepAnalysis":
            guard let category = record.category, let value = stages[category] else { throw SeedFailure(reason: "Unknown sleep stage") }
            try require(record.kind == "category" && b > a, "Invalid sleep interval")
            return HKCategorySample(type: type as! HKCategoryType, value: value.rawValue, start: a, end: b, metadata: md)
        case "workout":
            guard let duration = record.duration_seconds, let energy = record.energy_kcal, let distance = record.distance_m else { throw SeedFailure(reason: "Missing workout fields") }
            try require(record.kind == "workout" && record.workout_activity == "walking", "Unsupported workout")
            try require(b > a && duration.isFinite && duration == b.timeIntervalSince(a), "Workout duration mismatch")
            try require(energy.isFinite && distance.isFinite && energy >= 0 && distance >= 0, "Invalid workout totals")
            // Public API: totals belong to this workout; no separate quantity samples are saved.
            return HKWorkout(activityType: .walking, start: a, end: b, duration: duration,
                totalEnergyBurned: HKQuantity(unit: .kilocalorie(), doubleValue: energy),
                totalDistance: HKQuantity(unit: .meter(), doubleValue: distance), metadata: md)
        default: throw SeedFailure(reason: "Unsupported record")
        }
    }
    private func owned(_ type: HKSampleType, dataset: String) async throws -> [HKSample] {
        let predicate = NSCompoundPredicate(andPredicateWithSubpredicates: [
            HKQuery.predicateForObjects(from: HKSource.default()),
            HKQuery.predicateForObjects(withMetadataKey: prefix + "datasetID", allowedValues: [dataset])])
        return try await withCheckedThrowingContinuation { continuation in
            store.execute(HKSampleQuery(sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: samples ?? []) }
            })
        }
    }
    private func readAll(_ recipe: SeedRecipe) async throws -> [String: HKSample] {
        var result: [String: HKSample] = [:]
        for key in types.keys.sorted() {
            for sample in try await owned(types[key]!, dataset: recipe.dataset_id) {
                guard let id = sample.metadata?[prefix + "canonicalSampleID"] as? String else {
                    throw SeedFailure(reason: "Owned dataset contains sample without canonical ID")
                }
                try require(result[id] == nil, "Duplicate canonical ID in HealthKit")
                result[id] = sample
            }
        }
        return result
    }
    private func verify(_ actual: HKSample, expected: HKSample, record: SeedRecord) throws -> [String: Any] {
        try require(actual.sourceRevision.source.bundleIdentifier == HKSource.default().bundleIdentifier, "Wrong source")
        try require(actual.sampleType == expected.sampleType && actual.startDate == expected.startDate && actual.endDate == expected.endDate, "Type/interval mismatch: " + record.sample_id)
        if let uuid = manifest[record.sample_id] { try require(actual.uuid.uuidString == uuid, "UUID mismatch") }
        for (key, value) in expected.metadata ?? [:] {
            guard let received = actual.metadata?[key] as? NSObject, let expectedValue = value as? NSObject else {
                throw SeedFailure(reason: "Missing metadata: " + key)
            }
            try require(received.isEqual(expectedValue), "Metadata mismatch: " + key)
        }
        var row: [String: Any] = ["sampleID": record.sample_id, "uuid": actual.uuid.uuidString,
            "type": record.hk_type, "source": actual.sourceRevision.source.bundleIdentifier,
            "start": record.start_at, "end": record.end_at, "metadataVerified": true]
        if let actual = actual as? HKQuantitySample, let expected = expected as? HKQuantitySample {
            let unit: HKUnit = record.hk_type == "stepCount" ? .count() : .count().unitDivided(by: .minute())
            let value = actual.quantity.doubleValue(for: unit)
            try require(abs(value - expected.quantity.doubleValue(for: unit)) < 0.000001, "Quantity mismatch")
            row["value"] = value; row["unit"] = record.unit
        }
        if let actual = actual as? HKCategorySample, let expected = expected as? HKCategorySample {
            try require(actual.value == expected.value, "Sleep category mismatch")
            row["category"] = record.category; row["categoryValue"] = actual.value
        }
        if let actual = actual as? HKWorkout, let expected = expected as? HKWorkout {
            try require(actual.workoutActivityType == expected.workoutActivityType && actual.duration == expected.duration, "Workout activity/duration mismatch")
            guard let energy = actual.totalEnergyBurned?.doubleValue(for: .kilocalorie()),
                  let distance = actual.totalDistance?.doubleValue(for: .meter()) else {
                throw SeedFailure(reason: "Workout totals not preserved")
            }
            try require(abs(energy - (record.energy_kcal ?? -1)) < 0.000001 && abs(distance - (record.distance_m ?? -1)) < 0.000001, "Workout totals mismatch")
            row["duration_seconds"] = actual.duration; row["energy_kcal"] = energy
            row["distance_m"] = distance; row["workout_activity"] = "walking"
        }
        return row
    }
    func run(approvedDigest: String) async {
        guard !busy else { return }; busy = true; defer { busy = false }
        written = 0; reused = 0; manifest = [:]; digest = ""; recipe = nil
        do {
            let data = try Data(contentsOf: directory.appendingPathComponent("canonical-recipe.json"))
            digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
            try require(digest == approvedDigest && approvedDigest.count == 64, "Approved recipe digest mismatch; no write")
            let recipe = try JSONDecoder().decode(SeedRecipe.self, from: data); self.recipe = recipe
            try require(recipe.schema_version == 1 && recipe.recipe_version == 1 && recipe.timezone == "UTC", "Unsupported recipe version")
            try require(recipe.dataset_id == "six-month-pattern-demo-v1" && !recipe.samples.isEmpty, "Wrong dataset")
            let start = try date(recipe.start), end = try date(recipe.end)
            var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(secondsFromGMT: 0)!
            try require(calendar.date(byAdding: .month, value: -6, to: end) == start, "Not six calendar months")
            var expected = [String: HKSample](), records = [String: SeedRecord](), counts = [String: Int](), categories = [String: Int]()
            var asleep = [(Date, Date)]()
            // Validate every record before any authorization or save operation.
            for record in recipe.samples {
                try require(expected[record.sample_id] == nil, "Duplicate recipe sample ID")
                let sample = try native(record, recipe)
                expected[record.sample_id] = sample; records[record.sample_id] = record
                counts[record.hk_type, default: 0] += 1
                if let category = record.category {
                    categories[category, default: 0] += 1
                    if category.hasPrefix("asleep") { asleep.append((sample.startDate, sample.endDate)) }
                }
            }
            try require(counts == recipe.counts && categories == recipe.category_counts, "Recipe counts mismatch")
            asleep.sort { $0.0 < $1.0 }
            for index in asleep.indices.dropFirst() {
                try require(asleep[index-1].1 <= asleep[index].0, "Overlapping asleep stages")
            }
            let manifestURL = directory.appendingPathComponent("full-owned-manifest.json")
            if FileManager.default.fileExists(atPath: manifestURL.path) {
                let saved = try JSONSerialization.jsonObject(with: Data(contentsOf: manifestURL)) as? [String: Any]
                try require(saved?["sha256"] as? String == digest && saved?["datasetID"] as? String == recipe.dataset_id, "Manifest belongs to another recipe; no write")
                guard let ids = saved?["uuidsBySampleID"] as? [String: String] else { throw SeedFailure(reason: "Invalid manifest") }
                manifest = ids
            }
            try require(HKHealthStore.isHealthDataAvailable(), "HealthKit unavailable")
            for key in types.keys.sorted() {
                try require(store.authorizationStatus(for: types[key]!) == .sharingAuthorized, "Human write authorization needed: " + key)
            }
            var found = try await readAll(recipe)
            try require(Set(found.keys).isSubset(of: Set(expected.keys)), "Unexpected sample in owned dataset; no write")
            for id in found.keys.sorted() {
                _ = try verify(found[id]!, expected: expected[id]!, record: records[id]!)
                manifest[id] = found[id]!.uuid.uuidString; reused += 1
            }
            let missing = expected.keys.filter { found[$0] == nil }.sorted()
            try output("validated; saving bounded batches")
            for offset in stride(from: 0, to: missing.count, by: 200) {
                let ids = Array(missing[offset..<min(offset + 200, missing.count)])
                let batch = ids.map { expected[$0]! }
                // UUIDs are assigned by HealthKit construction, never invented by the recipe.
                for id in ids { manifest[id] = expected[id]!.uuid.uuidString }
                try checkpoint()
                try await store.save(batch)
                written += batch.count
                found = try await readAll(recipe)
                for id in ids {
                    guard let actual = found[id] else { throw SeedFailure(reason: "Missing saved UUID: " + id) }
                    _ = try verify(actual, expected: expected[id]!, record: records[id]!)
                }
                try checkpoint()
                try output("verified batch; \(written) new samples")
            }
            found = try await readAll(recipe)
            try require(Set(found.keys) == Set(expected.keys), "Final exact dataset membership mismatch")
            var rows = [[String: Any]]()
            for id in expected.keys.sorted() {
                rows.append(try verify(found[id]!, expected: expected[id]!, record: records[id]!))
            }
            try checkpoint()
            try output("verified", rows: rows)
        } catch {
            // Never delete pre-existing/probe samples or hide partially completed writes.
            // Saved UUID checkpoints allow scoped inspection and a safe retry.
            do { try output("failed; inspect owned manifest before retry", error: String(describing: error)) }
            catch { status = "Failed and could not write evidence: \(error)" }
        }
    }
}
