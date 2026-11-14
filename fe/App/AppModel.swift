import Foundation
import SwiftUI
import CompanionCore

@MainActor final class AppModel: ObservableObject {
    @Published private(set) var snapshot = LocalSnapshot()
    @Published private(set) var busy = false
    @Published var message: String?
    @Published private(set) var connected = false
    @Published private(set) var notificationPermission = "Not checked"
    private let client: CompanionClient?
    private let health = HealthStepsAdapter()
    private let reminders = LocalReminders()
    var state: CompanionCore.State? { snapshot.hidesCachedData ? nil : snapshot.state }
    var hasPending: Bool { !snapshot.outbox.isEmpty }

    init() {
        do {
            let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true).appendingPathComponent("LocalCompanion", isDirectory: true)
            let client = try CompanionClient(storage: ProtectedStorage(directory: directory))
            self.client = client; snapshot = client.snapshot
        } catch {
            client = nil
            message = "Local protected storage is unavailable. Unlock your device and relaunch. No cached records or pending writes were overwritten."
        }
    }
    private func publish() {
        guard let client else { return }
        let previousEpoch = snapshot.state?.data_epoch
        snapshot = client.snapshot
        if previousEpoch != snapshot.state?.data_epoch || hasPending { reminders.cancelAll() }
        if previousEpoch != nil && previousEpoch != snapshot.state?.data_epoch {
            message = "Server data epoch changed. Earlier cached records and pending requests were cleared, never relabeled."
        }
    }
    private func perform(network: Bool = true, _ action: (CompanionClient) async throws -> Void) async {
        guard !busy, let client else { return }
        busy = true; message = nil
        defer { busy = false }
        do {
            try await action(client); publish(); if network { connected = true }
            try await reminders.reconcile(state: state, locallyBlocked: hasPending)
            notificationPermission = reminders.permission
        } catch {
            publish()
            if network { connected = false }
            if error is URLError { message = "Backend unavailable. Saved requests keep their original IDs and epoch. Retry sync when the local service is back." }
            else if case ClientFailure.rejected(let code, _) = error { message = "Server rejected the request (\(code)). Refresh and review; requests are never retagged. Discard blocked pending changes before making a new edit." }
            else if case ClientFailure.pendingChanges = error { message = "Sync or explicitly discard pending changes before editing preferences or querying HealthKit." }
            else if case ClientFailure.expiredRequest = error { message = "Pending requests exceeded the 90-day receipt window and were cleared. Review the refreshed state before making new changes." }
            else { message = "Action could not complete safely. Refresh and review the current state; no new request identity was invented." }
            // Safe default when offline/uncertain: no stale reminder survives a failed reconciliation.
            reminders.cancelAll()
        }
    }
    func refresh() async { await perform { try await $0.synchronize() } }
    func discardPending() async {
        await perform { client in try client.discardPending(); try await client.refresh() }
    }
    private func submit<T: Encodable>(_ request: T, path: String, method: String = "POST", replacingQueue: Bool = false, client: CompanionClient) async throws {
        try client.enqueue(PendingMutation(path: path, method: method, request: request), replacingQueue: replacingQueue)
        publish(); reminders.cancelAll()
        try await client.synchronize()
    }
    func demo(action: String) async {
        await perform { client in
            guard let state = client.snapshot.state else { throw ClientFailure.noState }
            let request = DemoRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, action: action, fixture_set: "representative-v1", consent_demo: true)
            try await submit(request, path: "/demo", replacingQueue: action == "reset", client: client)
        }
    }
    func deleteData() async {
        reminders.cancelAll()
        await perform { client in
            guard let state = client.snapshot.state else { throw ClientFailure.noState }
            try await submit(DeleteRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, confirm: "delete_all"), path: "/data", method: "DELETE", replacingQueue: true, client: client)
        }
    }
    func process(scripted: Bool) async {
        await perform { client in
            guard client.snapshot.outbox.isEmpty else { throw ClientFailure.pendingChanges }
            guard let state = client.snapshot.state else { throw ClientFailure.noState }
            try await submit(ProcessRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, synthesis_mode: scripted ? "scripted" : "disabled"), path: "/process", client: client)
        }
    }
    func feedback(cardID: String, kind: String, value: String, scope: String = "card", event: EvidenceRef? = nil, attribution: String? = nil, note: String = "", expectedEpoch: String? = nil, expectedCardRevision: Int? = nil) async {
        await perform { client in
            guard let state = client.snapshot.state else { throw ClientFailure.noState }
            if let expectedEpoch, expectedEpoch != state.data_epoch { throw ClientFailure.rejected("stale_feedback_form", false) }
            if let expectedCardRevision, state.ledger.first(where: { $0.id == cardID })?.revision != expectedCardRevision { throw ClientFailure.rejected("stale_feedback_form", false) }
            let request = FeedbackRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, card_id: cardID, kind: kind, value: value, scope: scope, event_ref: event, attribution: attribution, note: String(note.prefix(1000)))
            try await submit(request, path: "/feedback", client: client)
        }
    }
    func savePreferences(_ preferences: Preferences, epoch: String, revision: Int) async {
        await perform { client in
            guard client.snapshot.outbox.isEmpty else { throw ClientFailure.pendingChanges }
            try await client.refresh()
            guard let current = client.snapshot.state, current.data_epoch == epoch, current.revision == revision else {
                throw ClientFailure.rejected("stale_preference_form", false)
            }
            try await submit(PreferencesRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: epoch, preferences: preferences), path: "/preferences", method: "PUT", client: client)
        }
    }
    func healthSync(connect: Bool) async {
        await perform { client in
            guard client.snapshot.outbox.isEmpty else { throw ClientFailure.pendingChanges }
            try await client.refresh()
            guard var state = client.snapshot.state else { throw ClientFailure.noState }
            if connect {
                var p = state.preferences
                if !p.enabled_source_ids.contains("healthkit") { p.enabled_source_ids.append("healthkit") }
                try await submit(PreferencesRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: state.data_epoch, preferences: p), path: "/preferences", method: "PUT", client: client)
                state = client.snapshot.state!
            }
            guard state.sources.contains(where: { $0.id == "healthkit" && $0.enabled && $0.consent == "granted" }) else { throw ClientFailure.rejected("consent_required", false) }
            let batch = await health.collect(state: state, requestAuthorization: connect)
            try await submit(batch, path: "/observations", client: client)
        }
    }
    func requestNotifications() async {
        await perform(network: false) { _ in try await reminders.requestPermission() }
    }
}
