import Foundation

/// This is transport normalization, not a health rule or an inference engine.
public enum DailySteps {
    public static func observation(value: Int?, date: String, timezone: String, existing: [Observation], newID: String = UUID().uuidString) -> Observation? {
        guard let value, value >= 0 else { return nil } // empty is unknown, never zero
        let old = existing.filter { $0.source_id == "healthkit" && $0.kind == "activity" && $0.local_date == date && $0.timezone == timezone }.max { $0.revision < $1.revision }
        if old?.activity?.value == value && old?.operation == "upsert" { return nil }
        let id = old?.id ?? newID
        return Observation(id: id, revision: (old?.revision ?? 0) + 1, source_id: "healthkit", mode: "live", operation: "upsert", kind: "activity", local_date: date, timezone: timezone, time_precision: "date", occurred_at: nil, lifecycle: "recorded", supersedes: old.map { EvidenceRef(observation_id: $0.id, revision: $0.revision) }, household_attribution: "self", provenance: Provenance(adapter: "healthkit_daily_steps", adapter_version: "1", record_id: old?.provenance.record_id ?? id, method: "daily_cumulative_sum", coverage: "partial"), purchase: nil, activity: Activity(metric: "steps", value: value, unit: "count"))
    }
    public static func update(readable: Bool, requested: Bool, window: Window, attemptedAt: String, error: String? = nil) -> SourceUpdate {
        SourceUpdate(source_id: "healthkit", authorization_requested: requested, read_access: error == nil && readable ? "samples_readable" : "unknown", query_status: error != nil ? "error" : (readable ? "success" : "empty"), attempted_at: attemptedAt, coverage_window: window, error_code: error)
    }
}

public struct NotificationPlan: Equatable, Sendable {
    public let cancel: [String]
    public let upsert: [NotificationIntent]
    public static func make(state: State, existing: [String], now: Date, authorized: Bool, locallyBlocked: Bool) -> NotificationPlan {
        let formatter = ISO8601DateFormatter()
        let desired = authorized && !locallyBlocked && state.preferences.notifications.enabled ? state.notification_intents.filter {
            guard let scheduled = formatter.date(from: $0.scheduled_at), let expires = formatter.date(from: $0.expires_at) else { return false }
            return scheduled > now && expires > now && expires > scheduled
        } : []
        let ids = Set(desired.map { identifier(epoch: state.data_epoch, intent: $0.id) })
        return NotificationPlan(cancel: existing.filter { !ids.contains($0) }, upsert: desired)
    }
    public static func identifier(epoch: String, intent: String) -> String { "companion:\(epoch):\(intent)" }
}
