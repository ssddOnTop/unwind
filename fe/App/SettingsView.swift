import SwiftUI
import CompanionCore

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @SwiftUI.State private var editing = false
    @SwiftUI.State private var confirmHealth = false
    @SwiftUI.State private var confirmDelete = false
    @SwiftUI.State private var confirmReset = false
    @SwiftUI.State private var confirmDiscard = false
    var body: some View {
        NavigationStack {
            List {
                Section("Persistent preferences") {
                    Text("Constraints and suppression are stored by the local backend, independently of AI. Refresh before editing; stale forms are rejected rather than silently replayed.")
                    Button("Refresh and edit preferences") {
                        Task { await model.refresh(); if model.connected && !model.hasPending { editing = true } }
                    }.disabled(model.state == nil || model.hasPending)
                }
                Section("Sources") {
                    ForEach(model.state?.sources ?? [], id: \.id) { source in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(source.id.capitalized).font(.headline)
                            Text("\(source.status) · capability \(source.capability)")
                            Text("App consent: \(source.consent) · enabled: \(source.enabled ? "yes" : "no")")
                            Text("Read access: \(source.read_access) · authorization requested: \(source.authorization_requested ? "yes" : "no")")
                            Text("Coverage: \(source.coverage)")
                            Text("Last attempt: \(source.last_attempt_at ?? "Unknown")\nLast success: \(source.last_success_at ?? "Unknown")").font(.caption)
                            if let reason = source.reason { Text(reason).font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                }
                Section("Foreground HealthKit") {
                    Text("Only daily step totals for seven completed local dates are queried. Enabling consents to sending normalized totals to your local backend. Apple's read permission is separate. No background collection or health writes.")
                    Text("An empty query means unknown—not denied access, zero activity, or deleted samples. Phone/watch reconciliation and device behavior still require real-device validation.").font(.caption)
                    Button("Enable sync and request HealthKit access") { confirmHealth = true }
                        .disabled(model.state == nil || model.hasPending)
                    Button("Read and sync daily steps now") { Task { await model.healthSync(connect: false) } }
                        .disabled(model.hasPending || !(model.state?.sources.contains { $0.id == "healthkit" && $0.enabled && $0.consent == "granted" } ?? false))
                    Text("Disconnect in Edit preferences. Revoking app consent stops use; historical server evidence remains until retention or Delete all data.").font(.caption)
                }
                Section("Optional local reminders") {
                    Text("iOS permission: \(model.notificationPermission)")
                    Text("Enable scheduling and choose time in preferences, then grant iOS permission separately. Only generic text appears on the lock screen. Scheduling is not proof of delivery.")
                    Text("iOS may defer or suppress delivery. Expired requests are removed on foreground sync; exact expiry while the app is terminated is not guaranteed.").font(.caption)
                    Button("Request iOS reminder permission") { Task { await model.requestNotifications() } }
                }
                Section("Offline queue") {
                    Text("\(model.snapshot.outbox.count) pending request(s)")
                    Button("Retry sync") { Task { await model.refresh() } }
                    Button("Discard pending changes", role: .destructive) { confirmDiscard = true }.disabled(!model.hasPending)
                    Text("Requests preserve exact bodies, IDs and epochs across restart. Old-epoch requests are discarded, never relabeled. No personal payload is logged.").font(.caption)
                }
                Section("Local prototype") {
                    Text("http://127.0.0.1:8000 · no authentication")
                    Text("Simulator-to-Mac topology. On a physical device, loopback points to the device itself. No LAN endpoint configuration is offered in this prototype.").font(.caption)
                    Text("Provider keys and model configuration belong only on the backend. Plaid and email are not configured. No iCloud writes.").font(.caption)
                }
                Section("Data controls") {
                    Button("Reset synthetic demo", role: .destructive) { confirmReset = true }.disabled(model.state == nil)
                    Button("Delete all application data", role: .destructive) { confirmDelete = true }.disabled(model.snapshot.state == nil)
                    Text("Delete rotates the server epoch and clears this client's cached records, pending mutations and notifications after confirmation. This never deletes records from Apple's Health app. An offline delete is pending, not completed.").font(.caption)
                }
            }.disabled(model.busy).navigationTitle("Settings")
                .sheet(isPresented: $editing) { if let state = model.state { PreferencesForm(model: model, state: state) } }
                .confirmationDialog("Consent to local-backend sync of daily step totals and request Apple's separate read permission?", isPresented: $confirmHealth, titleVisibility: .visible) {
                    Button("Enable and request access") { Task { await model.healthSync(connect: true) } }
                    Button("Cancel", role: .cancel) {}
                }
                .confirmationDialog("Delete all backend application data? Pending local writes will be discarded. This cannot be undone.", isPresented: $confirmDelete, titleVisibility: .visible) {
                    Button("Delete all application data", role: .destructive) { Task { await model.deleteData() } }
                    Button("Cancel", role: .cancel) {}
                }
                .confirmationDialog("Replace the synthetic demo and clear its feedback/preferences? The backend refuses reset when live data or consent exists.", isPresented: $confirmReset, titleVisibility: .visible) {
                    Button("Reset demo", role: .destructive) { Task { await model.demo(action: "reset") } }
                    Button("Cancel", role: .cancel) {}
                }
                .confirmationDialog("Discard saved pending requests? A previously sent request may already have committed; refresh will show server truth.", isPresented: $confirmDiscard, titleVisibility: .visible) {
                    Button("Discard pending changes", role: .destructive) { Task { await model.discardPending() } }
                    Button("Cancel", role: .cancel) {}
                }
        }
    }
}

struct PreferencesForm: View {
    @ObservedObject var model: AppModel
    @SwiftUI.State private var baseline: CompanionCore.State
    @SwiftUI.State private var draft: Preferences
    @Environment(\.dismiss) private var dismiss
    init(model: AppModel, state: CompanionCore.State) {
        self.model = model
        _baseline = SwiftUI.State(initialValue: state)
        _draft = SwiftUI.State(initialValue: state.preferences)
    }
    private func enabled(_ source: String) -> Binding<Bool> {
        Binding(get: { draft.enabled_source_ids.contains(source) }, set: { value in
            draft.enabled_source_ids.removeAll { $0 == source }
            if value { draft.enabled_source_ids.append(source) }
        })
    }
    private func suppressed(_ scenario: String) -> Binding<Bool> {
        Binding(get: { draft.suppressed_scenario_ids.contains(scenario) }, set: { value in
            draft.suppressed_scenario_ids.removeAll { $0 == scenario }
            if value { draft.suppressed_scenario_ids.append(scenario) }
        })
    }
    private var valid: Bool {
        TimeZone(identifier: draft.timezone) != nil && draft.notifications.local_time.range(of: "^([01][0-9]|2[0-3]):[0-5][0-9]$", options: .regularExpression) != nil && draft.notes.count <= 1000
    }
    var body: some View {
        NavigationStack {
            Form {
                Section("Suggestion preferences") {
                    Toggle("Food suggestions", isOn: $draft.food_suggestions)
                    Toggle("Activity suggestions", isOn: $draft.activity_suggestions)
                    Toggle("No kitchen access", isOn: $draft.no_kitchen)
                    Toggle("No spending", isOn: $draft.no_spending)
                    Toggle("Movement restriction", isOn: $draft.movement_restriction)
                }
                Section("Durable suppression") {
                    Toggle("Stop purchase suggestions", isOn: suppressed("purchase_recurrence"))
                    Toggle("Stop step suggestions", isOn: suppressed("recorded_steps"))
                    Toggle("Suppress preference scenario", isOn: suppressed("preference_filter"))
                }
                Section("Sources") {
                    Toggle("Use synthetic demo evidence", isOn: enabled("demo"))
                    if baseline.preferences.enabled_source_ids.contains("healthkit") { Toggle("Keep HealthKit sync enabled", isOn: enabled("healthkit")) }
                    Text("New HealthKit consent is requested separately in Settings. Plaid/email are unavailable.").font(.caption)
                }
                Section("Local time and reminders") {
                    TextField("IANA timezone", text: $draft.timezone).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Toggle("Schedule optional generic reminders", isOn: $draft.notifications.enabled)
                    TextField("Local time (HH:mm)", text: $draft.notifications.local_time).keyboardType(.numbersAndPunctuation)
                    Text("OS permission is separate. The backend decides eligibility and scheduling; the client only reconciles its desired set.").font(.caption)
                }
                Section("Explicit context") {
                    TextField("Optional note; no private records", text: $draft.notes, axis: .vertical)
                        .onChange(of: draft.notes) { _, new in if new.count > 1000 { draft.notes = String(new.prefix(1000)) } }
                    Text("Notes are context, not automatic medical rules.").font(.caption)
                    Toggle("Allow fixture-only cloud synthesis", isOn: $draft.cloud_consent)
                    Text("Default off. Requires backend-only provider setup; v1 does not authorize cloud transmission of live HealthKit evidence.").font(.caption)
                }
                Button("Save complete preferences") {
                    Task {
                        await model.savePreferences(draft, epoch: baseline.data_epoch, revision: baseline.revision)
                        dismiss()
                    }
                }.disabled(!valid || model.busy)
            }.navigationTitle("Preferences").toolbar { Button("Cancel") { dismiss() } }
        }
    }
}
