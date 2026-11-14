import SwiftUI
import CompanionCore

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @Binding var bankAccountsPresented: Bool
    let healthActions: ConnectionActions
    let manageAccount: () -> Void
    let signOut: () -> Void
    @SwiftUI.State private var confirmSignOut = false
    @SwiftUI.State private var editing = false
    @SwiftUI.State private var cloudConsent = false
    var body: some View {
        NavigationStack {
            List {
                if let message = model.message {
                    Section { Text(message); Button("Try again") { Task { await model.refresh() } } }
                }
                Section {
                    Button("Bank accounts") { bankAccountsPresented = true }
                        .accessibilityIdentifier("settings.bankAccounts")
                    Text("Manage banks and choose which accounts can provide new records.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Apple Health") {
                    ConnectionRow(connection: model.healthConnection, actions: healthActions)
                }
                if model.healthHasConsent {
                    Section { Button("Update step records") { Task { await model.healthSync(connect: false) } }.disabled(model.busy || model.hasPending) }
                }
                Section {
                    ConnectionRow(connection: model.reminderConnection,
                        actions: ConnectionActions(connect: { Task { await model.enableReminders() } },
                            disconnect: model.state?.preferences.notifications.enabled == true ? { Task { await model.stopReminders() } } : nil))
                    if model.state?.preferences.notifications.enabled == true {
                        Text("Reminders are on").font(.subheadline)
                    } else if model.reminderConnection.state == .connected {
                        Button("Turn on reminders") { Task { await model.enableReminders() } }
                    }
                } header: {
                    HStack { Text("Reminders"); Spacer(); AccessibleInfoPopover(title: "Reminders", message: "Optional, generic reminders. Turning them off cancels reminders from this app; iOS permission can be changed in iOS Settings. Delivery is never guaranteed.") }
                }
                CloudConsentSection(isEnabled: model.state?.preferences.cloud_consent ?? false, isBusy: model.busy || model.hasPending || model.state == nil,
                    onChange: { value in if value { cloudConsent = true } else { Task { await model.setCloudConsent(false) } } })
                Section {
                    Button("Preferences") { Task { await model.refresh(); if model.state != nil && !model.hasPending { editing = true } } }
                        .accessibilityIdentifier("Refresh and edit preferences")
                    NavigationLink("Your data") { DataControlsView(model: model) }
                    NavigationLink("Suggestion tools") { SuggestionToolsView(model: model) }
                    Button("Manage account", action: manageAccount)
                    Button("Sign out", role: .destructive) { confirmSignOut = true }
                    HStack { Text("Your signed-in account"); Spacer(); AccessibleInfoPopover(title: "Your privacy", message: "Sign-in is required. Connections remain optional and separate from cloud consent. This app stores normalized records locally with file protection and backup exclusion. Bank connections currently use synthetic Sandbox data. No provider keys or iCloud writes are used here.") }
                }
            }.navigationTitle("Settings").accessibilityIdentifier("app.settings")
                .navigationDestination(isPresented: $bankAccountsPresented) { BankingScreen(controller: model.banking) }
                .confirmationDialog("Sign out?", isPresented: $confirmSignOut, titleVisibility: .visible) {
                    Button("Sign out and clear this device", role: .destructive, action: signOut)
                    Button("Cancel", role: .cancel) {}
                } message: { Text("Clears local records and unsent changes before signing out. Server records stay in your account unless you delete them first.") }
                .sheet(isPresented: $editing) { if let state = model.state { PreferencesForm(model: model, state: state) } }
                .confirmationDialog("Allow cloud suggestions?", isPresented: $cloudConsent, titleVisibility: .visible) {
                    Button("Allow eligible records to be shared") { Task { await model.setCloudConsent(true) } }
                    Button("Cancel", role: .cancel) {}
                } message: { Text("Eligible server-provided records may be sent to an external AI service for optional wording. This does not authorize live Apple Health sharing. You can turn this off here anytime.") }
        }
    }
}

private struct SuggestionToolsView: View {
    @ObservedObject var model: AppModel
    @SwiftUI.State private var cloud = false
    var body: some View {
        List {
            Section("Update observations") {
                Button("Update observations") { Task { await model.process() } }.accessibilityIdentifier("Recompute on backend (no AI)")
                Button("Create cloud suggestions") { cloud = true }
                    .disabled(!(model.state?.preferences.cloud_consent ?? false) || (model.state?.observations.contains { $0.mode == "live" } ?? true))
                AccessibleInfoPopover(title: "Suggestions", message: "Observations use deterministic rules. Optional cloud wording uses a backend-configured free model, never paid fallback. If unavailable or rate limited, the observations remain available. Cloud consent is separate in Settings.")
                if let synthesis = model.state?.process.synthesis {
                    if synthesis.status == "running" { ProgressView("Preparing suggestions…") }
                    if synthesis.reason != nil { Text("Optional cloud wording is not available for this review.").font(.caption) }
                }
            }.disabled(model.busy || model.hasPending || model.state == nil)
        }.navigationTitle("Suggestion tools")
            .confirmationDialog("Create optional cloud wording?", isPresented: $cloud, titleVisibility: .visible) {
                Button("Create suggestions") { Task { await model.processLive() } }
                Button("Cancel", role: .cancel) {}
            } message: { Text("Only consented eligible Sandbox coverage summaries may be sent. Your observations remain available if the service cannot respond.") }
    }
}

private struct DataControlsView: View {
    @ObservedObject var model: AppModel
    @SwiftUI.State private var delete = false
    @SwiftUI.State private var discard = false
    var body: some View {
        List {
            if model.hasPending {
                Section("Changes waiting to save") {
                    Text("Some changes are saved on this device and haven’t reached the service yet.")
                    Button("Try saving again") { Task { await model.refresh() } }
                    Button("Discard unsent changes", role: .destructive) { discard = true }
                }
            }
            Section {
                Button("Delete all data", role: .destructive) { delete = true }.accessibilityIdentifier("Delete all application data").disabled(model.state == nil)
                Text("Deleting removes this app’s records and connections, not records in Apple Health. Unconfirmed remote bank cleanup will be reported.").font(.caption)
            }
        }.navigationTitle("Your data")
            .confirmationDialog("Delete all app data?", isPresented: $delete, titleVisibility: .visible) {
                Button("Delete all data", role: .destructive) { Task { await model.deleteData() } }.accessibilityIdentifier("Delete all application data")
                Button("Cancel", role: .cancel) {}
            } message: { Text("Records, connections and unsent changes will be removed. This cannot be undone. If offline, deletion remains pending until confirmed.") }
            .confirmationDialog("Discard unsent changes?", isPresented: $discard, titleVisibility: .visible) {
                Button("Discard changes", role: .destructive) { Task { await model.discardPending() } }
                Button("Cancel", role: .cancel) {}
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
                Section("Suggestions to pause") {
                    Toggle("Stop purchase suggestions", isOn: suppressed("purchase_recurrence"))
                    Toggle("Stop step suggestions", isOn: suppressed("recorded_steps"))
                    Toggle("Pause preference-based suggestions", isOn: suppressed("preference_filter"))
                }
                Section("Sources") {
                    if baseline.preferences.enabled_source_ids.contains("healthkit") { Toggle("Keep HealthKit sync enabled", isOn: enabled("healthkit")) }
                    Text("New HealthKit consent is requested separately. Use the Plaid Sandbox section to connect or revoke bank consent; editing this form cannot establish a bank connection. Email remains unavailable.").font(.caption)
                }
                Section("Local time and reminders") {
                    TextField("IANA timezone", text: $draft.timezone).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Toggle("Schedule optional generic reminders", isOn: $draft.notifications.enabled)
                    TextField("Local time (HH:mm)", text: $draft.notifications.local_time).keyboardType(.numbersAndPunctuation)
                    Text("OS permission is separate. The backend decides eligibility and scheduling; the client only reconciles its desired set.").font(.caption)
                }
                Section("Optional context") {
                    TextField("Optional note; no private records", text: $draft.notes, axis: .vertical)
                        .onChange(of: draft.notes) { _, new in if new.count > 1000 { draft.notes = String(new.prefix(1000)) } }
                    Text("Notes are context, not automatic medical rules.").font(.caption)
                }
                Button("Save preferences") {
                    Task {
                        await model.savePreferences(draft, epoch: baseline.data_epoch, revision: baseline.revision)
                        dismiss()
                    }
                }.accessibilityIdentifier("Save complete preferences").disabled(!valid || model.busy)
            }.navigationTitle("Preferences").toolbar { Button("Cancel") { dismiss() } }
        }
    }
}
