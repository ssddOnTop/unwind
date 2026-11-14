import Foundation
import HealthKit
import UIKit
import CryptoKit
import CompanionCore

@MainActor final class HealthStepsAdapter {
    private let store = HKHealthStore()
    private var generation = 0
    private var queries: [UUID: HKQuery] = [:]
    private var continuations: [UUID: CheckedContinuation<Int?, Error>] = [:]
    private var sampleContinuations: [UUID: CheckedContinuation<[HKSample], Error>] = [:]
    func cancelAll() {
        generation += 1
        for query in queries.values { store.stop(query) }; queries.removeAll()
        let pending = continuations.values; continuations.removeAll()
        for continuation in pending { continuation.resume(throwing: CancellationError()) }
        let samplePending = sampleContinuations.values; sampleContinuations.removeAll()
        for continuation in samplePending { continuation.resume(throwing: CancellationError()) }
    }
    private let steps = HKQuantityType.quantityType(forIdentifier: .stepCount)!

    /// Only called by an explicit foreground button. No HealthKit writes or background delivery.
    func collect(state: CompanionCore.State, requestAuthorization: Bool) async throws -> ObservationBatch {
        let capturedGeneration = generation
        let zone = TimeZone(identifier: state.preferences.timezone) ?? .current
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = zone
        let today = calendar.startOfDay(for: Date())
        let start = calendar.date(byAdding: .day, value: -7, to: today)!
        let end = calendar.date(byAdding: .day, value: -1, to: today)!
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar; formatter.timeZone = zone; formatter.dateFormat = "yyyy-MM-dd"
        let window = Window(start_date: formatter.string(from: start), end_date: formatter.string(from: end), timezone: zone.identifier)
        var requested = state.sources.first { $0.id == "healthkit" }?.authorization_requested ?? false
        var observations: [Observation] = []
        var readable = false
        var failure: String?
        if !HKHealthStore.isHealthDataAvailable() { failure = "unavailable" }
        else if !UIApplication.shared.isProtectedDataAvailable { failure = "protected_data" }
        else {
            do {
                if requestAuthorization {
                    requested = true
                    // Completion never establishes read permission; only nonempty query results are readable.
                    try await store.requestAuthorization(toShare: [], read: [steps])
                }
                guard capturedGeneration == generation else { throw CancellationError() }
                for dayOffset in 0..<7 {
                    try Task.checkCancellation()
                    guard capturedGeneration == generation else { throw CancellationError() }
                    let day = calendar.date(byAdding: .day, value: dayOffset, to: start)!
                    let next = calendar.date(byAdding: .day, value: 1, to: day)!
                    let value = try await dailyTotal(start: day, end: next)
                    if value != nil { readable = true }
                    if let o = DailySteps.observation(value: value, date: formatter.string(from: day), timezone: zone.identifier, existing: state.observations) { observations.append(o) }
                }
            } catch {
                if error is CancellationError || generation != capturedGeneration { throw CancellationError() }
                // No raw HealthKit errors, samples, or personal counts enter logs.
                failure = UIApplication.shared.isProtectedDataAvailable ? "query_failed" : "protected_data"
                observations.removeAll(); readable = false
            }
        }
        guard capturedGeneration == generation else { throw CancellationError() }
        let update = DailySteps.update(readable: readable, requested: requested, window: window, attemptedAt: ISO8601DateFormatter().string(from: Date()), error: failure)
        return ObservationBatch(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, observations: observations, source_updates: [update])
    }

    private func dailyTotal(start: Date, end: Date) async throws -> Int? {
        let id = UUID()
        return try await withCheckedThrowingContinuation { continuation in
            continuations[id] = continuation
            let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
            let query = HKStatisticsQuery(quantityType: steps, quantitySamplePredicate: predicate, options: .cumulativeSum) { [weak self] _, result, error in
                let outcome: Result<Int?, Error>
                if let error { outcome = .failure(error) }
                else if let quantity = result?.sumQuantity() {
                    let count = quantity.doubleValue(for: .count())
                    outcome = count.isFinite && count >= 0 && count < Double(Int.max) ? .success(Int(count.rounded())) : .failure(ClientFailure.invalidResponse)
                } else { outcome = .success(nil) }
                Task { @MainActor [weak self] in
                    guard let self, let continuation = self.continuations.removeValue(forKey: id) else { return }
                    self.queries.removeValue(forKey: id); continuation.resume(with: outcome)
                }
            }
            queries[id] = query; store.execute(query)
        }
    }
    /// Explicit six-calendar-month foreground refresh. Permissions belong to THIS app.
    /// Samples are scoped to the captured account epoch by the caller; no writes/background delivery.
    func collectNormalized(state: CompanionCore.State, requestAuthorization: Bool) async throws -> [HealthIngestRequest] {
        guard HKHealthStore.isHealthDataAvailable(), UIApplication.shared.isProtectedDataAvailable else { throw ClientFailure.rejected("not_configured", false) }
        let captured = generation
        let sleep = HKCategoryType.categoryType(forIdentifier: .sleepAnalysis)!
        let heart = HKQuantityType.quantityType(forIdentifier: .heartRate)!
        let resting = HKQuantityType.quantityType(forIdentifier: .restingHeartRate)!
        let workout = HKObjectType.workoutType()
        let types: [HKSampleType] = [steps, sleep, heart, resting, workout]
        if requestAuthorization { try await store.requestAuthorization(toShare: [], read: Set(types)) }
        guard captured == generation else { throw CancellationError() }
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let end = Date(), start = calendar.date(byAdding: .month, value: -6, to: calendar.startOfDay(for: end))!
        var normalized: [NormalizedHealthSample] = []
        for type in types {
            try Task.checkCancellation()
            guard captured == generation else { throw CancellationError() }
            let rows = try await readSamples(type: type, start: start, end: end)
            guard captured == generation else { throw CancellationError() }
            if type == steps {
                // Separate real-device daily totals from the explicit helper cohort. Never sum phone+watch totals.
                var groups: [String: [HKQuantitySample]] = [:]
                for case let sample as HKQuantitySample in rows {
                    guard let origin = origin(sample) else { continue }
                    if origin == "synthetic" {
                        if let value = normalize(sample, kind: "dailyCumulative", hkType: "stepCount", value: sample.quantity.doubleValue(for: .count()), unit: "count") { normalized.append(value) }
                    } else {
                        // Device identity is used only in memory to separate phone/watch streams, never transmitted.
                        let stream = sample.sourceRevision.source.bundleIdentifier + ":" + (sample.device?.localIdentifier ?? sample.sourceRevision.productType ?? "unknown-device")
                        groups[stream, default: []].append(sample)
                    }
                }
                // Each source's interval samples are apportioned to UTC days. Select the largest source total per day,
                // not a sum across devices. This conservative source-selection total is explicitly labeled in provenance.
                var perDay: [Date: (Double, HKQuantitySample)] = [:]
                for samples in groups.values {
                    var totals: [Date: Double] = [:]
                    for sample in samples {
                        let count = sample.quantity.doubleValue(for: .count())
                        guard count.isFinite, count >= 0 else { continue }
                        let duration = sample.endDate.timeIntervalSince(sample.startDate)
                        var day = calendar.startOfDay(for: max(start, sample.startDate))
                        while day <= min(end, sample.endDate) {
                            let next = calendar.date(byAdding: .day, value: 1, to: day)!
                            let overlap = max(0, min(end, min(next, sample.endDate)).timeIntervalSince(max(day, sample.startDate)))
                            let part = duration > 0 ? count * overlap / duration : (day == calendar.startOfDay(for: sample.startDate) ? count : 0)
                            if part > 0 || duration == 0 { totals[day, default: 0] += part }
                            day = next
                        }
                    }
                    for (day,total) in totals {
                        if perDay[day] == nil || total > perDay[day]!.0 { perDay[day] = (total, samples[0]) }
                    }
                }
                for (day, selected) in perDay {
                    guard var value = normalize(selected.1, kind: "dailyCumulative", hkType: "stepCount", value: selected.0, unit: "count") else { continue }
                    value.timezone = "UTC"
                    value.start_at = stamp(day); value.end_at = stamp(min(end, calendar.date(byAdding: .day, value: 1, to: day)!))
                    guard value.start_at != value.end_at else { continue }
                    value.sample_id = stable("daily-steps:" + stamp(day))
                    value.source_bundle = "healthkit.daily-source-selection"
                    value.source_name = "HealthKit daily maximum source total (not combined devices)"
                    value.source_revision = "daily-max-source-v1"
                    normalized.append(value)
                }
            } else {
                for sample in rows {
                    guard origin(sample) != nil else { continue }
                    if let category = sample as? HKCategorySample {
                        let mapping: [Int:String] = [HKCategoryValueSleepAnalysis.inBed.rawValue:"inBed", HKCategoryValueSleepAnalysis.awake.rawValue:"awake", HKCategoryValueSleepAnalysis.asleepCore.rawValue:"asleepCore", HKCategoryValueSleepAnalysis.asleepDeep.rawValue:"asleepDeep", HKCategoryValueSleepAnalysis.asleepREM.rawValue:"asleepREM", HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue:"asleepUnspecified"]
                        if let stage = mapping[category.value], let value = normalize(sample, kind: "category", hkType: "sleepAnalysis", category: stage) { normalized.append(value) }
                    } else if let quantity = sample as? HKQuantitySample {
                        if let value = normalize(sample, kind: "quantity", hkType: type == heart ? "heartRate" : "restingHeartRate", value: quantity.quantity.doubleValue(for: HKUnit.count().unitDivided(by: .minute())), unit: "bpm") { normalized.append(value) }
                    } else if let activity = sample as? HKWorkout {
                        let mapping: [HKWorkoutActivityType:String] = [.walking:"walking", .running:"running", .cycling:"cycling"]
                        if let name = mapping[activity.workoutActivityType], var value = normalize(sample, kind: "workout", hkType: "workout") {
                            value.workout_activity = name; value.duration_seconds = activity.duration
                            value.energy_kcal = activity.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()?.doubleValue(for: .kilocalorie())
                            value.distance_m = activity.totalDistance?.doubleValue(for: .meter())
                            normalized.append(value)
                        }
                    }
                }
            }
            guard normalized.count <= 50_000 else { throw ClientFailure.rejected("invalid_request", false) }
        }
        guard captured == generation else { throw CancellationError() }
        return try HealthNormalization.batches(samples: normalized.sorted { $0.sample_id < $1.sample_id }, epoch: state.data_epoch, start: start, end: end)
    }
    private func stamp(_ value: Date) -> String { let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f.string(from: value) }
    private func stable(_ value: String) -> String { SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined() }
    private func origin(_ sample: HKSample) -> String? {
        let prefix = HealthNormalization.helperBundle, metadata = sample.metadata ?? [:]
        let synthetic = HealthNormalization.syntheticFlag(metadata[prefix + ".synthetic"])
        return HealthNormalization.provenance(bundle: sample.sourceRevision.source.bundleIdentifier, synthetic: synthetic,
            dataset: metadata[prefix + ".datasetID"] as? String, recipe: metadata[prefix + ".recipe"] as? String, declaredTimezone: metadata[prefix + ".timezone"] as? String)
    }
    private func normalize(_ sample: HKSample, kind: String, hkType: String, value: Double? = nil, unit: String? = nil, category: String? = nil) -> NormalizedHealthSample? {
        guard let provenance = origin(sample), value?.isFinite ?? true, (value ?? 0) >= 0 else { return nil }
        let source = sample.sourceRevision.source
        let synthetic = provenance == "synthetic"
        let recordedZone = sample.metadata?[HKMetadataKeyTimeZone] as? String
        let zone = !synthetic && recordedZone.flatMap(TimeZone.init(identifier:)) != nil ? recordedZone! : "UTC"
        return NormalizedHealthSample(sample_id: stable(source.bundleIdentifier + ":" + sample.uuid.uuidString), hk_type: hkType, kind: kind,
            start_at: stamp(sample.startDate), end_at: stamp(sample.endDate), timezone: zone, value: value, unit: unit, category: category,
            workout_activity: nil, duration_seconds: nil, energy_kcal: nil, distance_m: nil,
            source_name: synthetic ? "Synthetic HealthKit helper" : String(source.name.prefix(80)), revision: 1,
            provenance: provenance, source_bundle: source.bundleIdentifier, source_revision: String((sample.sourceRevision.version ?? "unknown").prefix(80)),
            dataset_id: synthetic ? HealthNormalization.dataset : nil, recipe: synthetic ? HealthNormalization.recipe : nil)
    }
    private func readSamples(type: HKSampleType, start: Date, end: Date) async throws -> [HKSample] {
        let id = UUID()
        return try await withCheckedThrowingContinuation { continuation in
            sampleContinuations[id] = continuation
            // Overlap predicate includes cross-midnight sleep; never infer access from authorization completion.
            let query = HKSampleQuery(sampleType: type, predicate: HKQuery.predicateForSamples(withStart: start, end: end), limit: 50_001, sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]) { [weak self] _, samples, error in
                Task { @MainActor [weak self] in
                    guard let self, let pending = self.sampleContinuations.removeValue(forKey: id) else { return }
                    self.queries.removeValue(forKey: id)
                    if error != nil || (samples?.count ?? 0) > 50_000 { pending.resume(throwing: ClientFailure.invalidResponse) }
                    else { pending.resume(returning: samples ?? []) }
                }
            }
            queries[id] = query; store.execute(query)
        }
    }
}
