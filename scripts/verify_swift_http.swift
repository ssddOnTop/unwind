// Run with: be/.venv/bin/python evidence/swift-http/run.py
// CompanionClient fixes port 8000; this adapter changes ONLY the destination port.
import Foundation
import CompanionCore

actor IsolatedTransport: HTTPTransport {
    let port: Int
    let real = URLSessionTransport()
    var calls = 0
    var routes: [String: Int] = [:]
    init(port: Int) { self.port = port }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        guard let url = request.url, url.host == "127.0.0.1", url.port == 8000 else {
            throw ClientFailure.invalidEndpoint
        }
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        components.port = port
        var forwarded = request
        forwarded.url = components.url!
        calls += 1
        routes["\(request.httpMethod ?? "GET") \(url.path)", default: 0] += 1
        return try await real.send(forwarded)
    }
}

struct ProbeFailure: Error { let label: String }
@main @MainActor struct Probe {
    static var passed: [String] = []
    static func check(_ condition: Bool, _ label: String) throws {
        guard condition else { throw ProbeFailure(label: label) }
        passed.append(label)
    }
    static func state(_ client: CompanionClient) throws -> State {
        guard let state = client.snapshot.state else { throw ProbeFailure(label: "missing_state") }
        return state
    }
    static func mutate<T: Encodable>(_ client: CompanionClient, _ path: String, _ body: T, method: String = "POST") async throws -> State {
        try client.enqueue(PendingMutation(path: path, method: method, request: body))
        try await client.synchronize()
        try check(client.snapshot.outbox.isEmpty, "outbox_drained_\(path)")
        return try state(client)
    }
    static func pristine(_ state: State) -> Bool {
        state.observations.isEmpty && state.feedback.isEmpty && state.notification_intents.isEmpty
            && state.process.status == "idle" && state.sources.allSatisfy { $0.consent == "not_requested" }
            && state.preferences.notes.isEmpty && state.preferences.suppressed_scenario_ids.isEmpty
    }
    static func main() async {
        var transport: IsolatedTransport?
        var failure: String?
        do {
            let args = CommandLine.arguments
            guard args.count == 4, let port = Int(args[1]), [8767, 8768].contains(port) else {
                throw ProbeFailure(label: "isolated_runner_arguments_required")
            }
            let directory = URL(fileURLWithPath: args[2], isDirectory: true)
            // Runner creates a fresh server directory and ownership token before launch.
            let token = try String(contentsOf: directory.appendingPathComponent("ownership"), encoding: .utf8)
            try check(token == args[3], "owned_instance_token")
            let wire = IsolatedTransport(port: port); transport = wire
            let storage = ProtectedStorage(directory: directory.appendingPathComponent("client"))
            let client = try CompanionClient(storage: storage, transport: wire)
            try await client.refresh()
            let initial = try state(client)
            // Absolutely no seed, mutation, or delete unless the initial instance is pristine.
            try check(pristine(initial), "pristine_guard_before_all_writes")
            try await client.refresh()
            try check(try state(client) == initial, "get_state_is_read_only")
            let epoch = initial.data_epoch
            let seed = DemoRequest(schema_version: "1", request_id: "swift-seed", data_epoch: epoch,
                action: "seed", fixture_set: "representative-v1", consent_demo: true)
            let seeded = try await mutate(client, "/demo", seed)
            try check(seeded.data_epoch == epoch && seeded.revision > initial.revision, "seed_epoch_revision")
            try check(!pristine(seeded), "pristine_guard_rejects_real_populated_response")
            try check(seeded.observations.count == 6 && seeded.observations.allSatisfy {
                $0.mode == "fixture" && $0.source_id == "demo" && $0.provenance.method == "synthetic"
            }, "six_synthetic_observations")
            try check(seeded.ledger.first { $0.id == "purchase_recurrence" }?.purchase_count == 3,
                "purchase_total_three")
            try check(seeded.ledger.first { $0.id == "recorded_steps" }?.recorded_steps == 13800,
                "steps_total_13800")
            try check(seeded.ledger.first { $0.id == "recorded_steps" }?.suggestion?.status != "eligible",
                "movement_restriction_immediate")
            let replay = try await mutate(client, "/demo", seed)
            try check(replay == seeded, "seed_idempotent_no_revision_bump")
            for mode in ["disabled", "scripted"] {
                let before = try state(client)
                let processed = try await mutate(client, "/process", ProcessRequest(schema_version: "1",
                    request_id: "swift-process-\(mode)", data_epoch: epoch, synthesis_mode: mode))
                try check(processed.data_epoch == epoch && processed.revision > before.revision,
                    "process_\(mode)_epoch_revision")
                try check(processed.process.status == "completed" && processed.process.ledger_status == "completed",
                    "process_\(mode)_ledger_completed")
                try check(processed.observations == seeded.observations && processed.preferences == seeded.preferences,
                    "process_\(mode)_preserves_evidence_preferences")
                if mode == "disabled" {
                    try check(processed.process.synthesis.status == "skipped" && processed.process.synthesis.reason == "disabled",
                        "disabled_synthesis_skipped")
                } else {
                    try check(processed.process.synthesis.status == "completed" && !processed.process.synthesis.drafts.isEmpty,
                        "scripted_sdk_completed_with_drafts")
                    try check(processed.process.synthesis.drafts.allSatisfy { draft in
                        draft.card_ids.allSatisfy { id in processed.ledger.contains { $0.id == id } }
                        && draft.evidence.allSatisfy { ref in processed.observations.contains { $0.id == ref.observation_id && $0.revision == ref.revision } }
                    }, "scripted_draft_references_resolve")
                }
            }
            var preferences = seeded.preferences
            preferences.food_suggestions = false
            preferences.notes = "SYNTHETIC_SWIFT_PRIVATE_NOTE_CANARY"
            let beforePreferences = try state(client)
            let saved = try await mutate(client, "/preferences", PreferencesRequest(schema_version: "1",
                request_id: "swift-preferences", data_epoch: epoch, preferences: preferences), method: "PUT")
            try check(saved.preferences == preferences && saved.data_epoch == epoch && saved.revision > beforePreferences.revision,
                "preferences_put_roundtrip_epoch_revision")
            try check(saved.ledger.first { $0.id == "purchase_recurrence" }?.suggestion?.status != "eligible",
                "food_preference_immediate_filter")
            let feedback = FeedbackRequest(schema_version: "1", request_id: "swift-feedback", data_epoch: epoch,
                card_id: "purchase_recurrence", kind: "suppress", value: "stop", scope: "scenario",
                event_ref: nil, attribution: nil, note: "SYNTHETIC_SWIFT_FEEDBACK_CANARY")
            let stopped = try await mutate(client, "/feedback", feedback)
            try check(stopped.data_epoch == epoch && stopped.revision > saved.revision && stopped.feedback.count == 1,
                "feedback_post_epoch_revision")
            try check(stopped.preferences.suppressed_scenario_ids.contains("purchase_recurrence")
                && stopped.ledger.first { $0.id == "purchase_recurrence" }?.suggestion?.status != "eligible"
                && stopped.notification_intents.isEmpty && stopped.observations == seeded.observations,
                "suppression_preserves_facts_cancels_intents")
            let feedbackReplay = try await mutate(client, "/feedback", feedback)
            try check(feedbackReplay == stopped, "feedback_idempotent_no_revision_bump")
            try check(try storage.load() == client.snapshot, "real_protected_storage_roundtrip")
            let deletion = try PendingMutation(path: "/data", method: "DELETE", request: DeleteRequest(
                schema_version: "1", request_id: "swift-delete", data_epoch: epoch, confirm: "delete_all"))
            try client.enqueue(deletion)
            try check(client.snapshot.hidesCachedData, "queued_delete_hides_cached_data")
            // The following queued old-epoch write must die when DELETE rotates the epoch.
            try client.enqueue(PendingMutation(path: "/feedback", request: FeedbackRequest(schema_version: "1",
                request_id: "swift-stale-queued", data_epoch: epoch, card_id: "purchase_recurrence", kind: "suppress",
                value: "stop", scope: "scenario", event_ref: nil, attribution: nil, note: "")))
            try await client.synchronize()
            let clean = try state(client)
            try check(clean.data_epoch != epoch && clean.observations.isEmpty && clean.feedback.isEmpty
                && clean.notification_intents.isEmpty && clean.preferences == initial.preferences
                && clean.process.status == "idle" && clean.sources.allSatisfy { $0.consent == "not_requested" },
                "delete_rotates_epoch_restores_clean_defaults")
            try check(client.snapshot.outbox.isEmpty && !client.snapshot.hidesCachedData,
                "delete_discards_old_epoch_queue")
            do {
                try client.enqueue(deletion)
                throw ProbeFailure(label: "stale_epoch_was_accepted")
            } catch ClientFailure.rejected("epoch_mismatch", false) {
                passed.append("stale_epoch_rejected_locally")
            }
            try await client.refresh()
            try check(try state(client) == clean, "post_delete_get_read_only")
            try check(try storage.load() == client.snapshot, "deleted_cache_persisted_clean")
        } catch let error as ProbeFailure { failure = error.label }
        catch let error as ClientFailure {
            if case .rejected(let code, _) = error { failure = "client_rejected_\(code)" }
            else { failure = "client_failure" }
        } catch { failure = "compile_runtime_or_wire_error" } // Never print private error payloads.
        let count = await transport?.calls ?? 0
        let routes = await transport?.routes ?? [:]
        let result: [String: Any] = ["passed": passed, "passed_count": passed.count,
            "failed_count": failure == nil ? 0 : 1, "failure": failure as Any? ?? NSNull(),
            "http_requests": count, "routes": routes, "client": "CompanionClient", "transport": "URLSessionTransport; destination port only remapped"]
        let data = try! JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
        print(String(decoding: data, as: UTF8.self))
        if failure != nil { exit(1) }
    }
}
