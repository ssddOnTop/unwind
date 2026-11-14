import Foundation
import HealthKit
import UIKit
import CompanionCore

@MainActor final class HealthStepsAdapter {
    private let store = HKHealthStore()
    private let steps = HKQuantityType.quantityType(forIdentifier: .stepCount)!

    /// Only called by an explicit foreground button. No HealthKit writes or background delivery.
    func collect(state: CompanionCore.State, requestAuthorization: Bool) async -> ObservationBatch {
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
                for dayOffset in 0..<7 {
                    let day = calendar.date(byAdding: .day, value: dayOffset, to: start)!
                    let next = calendar.date(byAdding: .day, value: 1, to: day)!
                    let value = try await dailyTotal(start: day, end: next)
                    if value != nil { readable = true }
                    if let o = DailySteps.observation(value: value, date: formatter.string(from: day), timezone: zone.identifier, existing: state.observations) { observations.append(o) }
                }
            } catch {
                // No raw HealthKit errors, samples, or personal counts enter logs.
                failure = UIApplication.shared.isProtectedDataAvailable ? "query_failed" : "protected_data"
                observations.removeAll(); readable = false
            }
        }
        let update = DailySteps.update(readable: readable, requested: requested, window: window, attemptedAt: ISO8601DateFormatter().string(from: Date()), error: failure)
        return ObservationBatch(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, observations: observations, source_updates: [update])
    }

    private func dailyTotal(start: Date, end: Date) async throws -> Int? {
        try await withCheckedThrowingContinuation { continuation in
            let predicate = HKQuery.predicateForSamples(withStart: start, end: end, options: .strictStartDate)
            // HealthKit cumulative statistics, not separate phone/watch totals or raw-sample summation.
            let query = HKStatisticsQuery(quantityType: steps, quantitySamplePredicate: predicate, options: .cumulativeSum) { _, result, error in
                if let error { continuation.resume(throwing: error); return }
                guard let quantity = result?.sumQuantity() else { continuation.resume(returning: nil); return }
                let count = quantity.doubleValue(for: .count())
                guard count.isFinite, count >= 0, count < Double(Int.max) else {
                    continuation.resume(throwing: ClientFailure.invalidResponse); return
                }
                continuation.resume(returning: Int(count.rounded()))
            }
            store.execute(query)
        }
    }
}
