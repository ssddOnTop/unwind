import SwiftUI
import CompanionCore

/// Insert only after the authenticated account model is admitted. Loading/errors never reveal Content.
struct OnboardingGate<Content: View>: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var controller: OnboardingController
    let manageAccount: () -> Void
    let signOut: () -> Void
    private let content: () -> Content
    init(model: AppModel, manageAccount: @escaping () -> Void, signOut: @escaping () -> Void,
         @ViewBuilder content: @escaping () -> Content) {
        self.model = model; self.controller = model.onboarding
        self.manageAccount = manageAccount; self.signOut = signOut; self.content = content
    }
    var body: some View {
        Group {
            if controller.canShowMain {
                content().task { await model.refresh() }
            }
            else { OnboardingView(model: model, controller: controller, manageAccount: manageAccount, signOut: signOut) }
        }
        .task(id: ObjectIdentifier(model)) { await controller.load() }
    }
}

struct OnboardingView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var controller: OnboardingController
    let manageAccount: () -> Void
    let signOut: () -> Void
    @SwiftUI.State private var showingBanks = false
    private var blocked: Bool { controller.busy || controller.hasRetry || model.busy }
    private var number: Int { [.health, .bank, .notifications, .initial_sync].firstIndex(of: controller.overview?.current_step ?? .health).map { $0 + 1 } ?? 4 }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    Text(controller.completionSummary == nil ? "Make this useful for you" : "Setup complete").font(.largeTitle.bold())
                    if controller.completionSummary == nil {
                        Text("Choose which context to share. Each connection is optional, and you can change it later in Settings.")
                            .foregroundStyle(.secondary)
                    }
                    if let value = controller.overview {
                        if value.completed {
                            if let summary = controller.completionSummary { completion(summary) }
                        } else {
                        ProgressView(value: Double(number), total: 4).accessibilityLabel("Setup step \(number) of 4")
                        Text("Step \(number) of 4 · Optional setup").font(.subheadline).foregroundStyle(.secondary)
                        if let step = value.current {
                            Text(verbatim: step.title).font(.title2.bold())
                            Text(verbatim: step.purpose)
                        }
                        switch value.current_step {
                        case .health: health
                        case .bank: bank
                        case .notifications: notifications
                        case .initial_sync: initialSync(value)
                        case .complete: EmptyView()
                        }
                        if let message = controller.deviceMessage { Text(verbatim: message).font(.callout).accessibilityIdentifier("onboarding.deviceStatus") }
                        if let progress = model.healthProgress { ProgressView(progress) }
                        if value.current_step != .initial_sync && value.current_step != .complete { decisions }
                        }
                    } else if controller.busy {
                        ProgressView("Loading your saved setup…")
                    }
                    if let issue = controller.issue {
                        Text(verbatim: issue).foregroundStyle(.secondary).accessibilityIdentifier("onboarding.issue")
                    }
                    if controller.hasRetry {
                        Button("Retry the same decision") { Task { await controller.retryDecision() } }.disabled(controller.busy)
                    }
                    if controller.overview == nil || controller.issue != nil {
                        Button("Reload saved progress") { Task { await controller.load() } }.disabled(controller.busy)
                    }
                    if controller.busy { ProgressView("Working securely…") }
                    Text("Setup choices are saved by your service. Device permission, a bank connection, and uploaded records are checked separately.")
                        .font(.footnote).foregroundStyle(.secondary)
                }.padding(24).frame(maxWidth: 620, alignment: .leading).frame(maxWidth: .infinity)
            }
            .navigationTitle("Welcome")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu("Account") {
                        Button("Manage sign-in", action: manageAccount)
                        Button("Sign out", role: .destructive, action: signOut)
                    }
                }
            }
            .sheet(isPresented: $showingBanks, onDismiss: {
                if model.banking.hasLinkSession { model.banking.cancelLink() }
            }) {
                NavigationStack {
                    BankingScreen(controller: model.banking)
                        .navigationTitle("Sandbox bank")
                        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showingBanks = false }.disabled(model.banking.working) } }
                        .overlay { OnboardingBankPresentation(controller: model.banking) }
                }.interactiveDismissDisabled(model.banking.working)
            }
        }.accessibilityIdentifier("onboarding.gate")
    }
    private var health: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Steps, sleep, heart rate, resting heart rate, and workouts", systemImage: "heart.text.square")
            Text("Apple lets you choose each read permission. An empty result can mean no records or access turned off; we never label it authorized or denied from an empty read.")
                .font(.callout).foregroundStyle(.secondary)
            Button("Choose Apple Health access and upload") {
                Task { await controller.runDeviceAction { await model.onboardingHealthUpload() } }
            }.buttonStyle(.borderedProminent).disabled(blocked).accessibilityIdentifier("onboarding.health.connect")
            Text("This starts the existing private Health uploader. Interrupted uploads retain their original account-scoped batches for retry.").font(.footnote)
        }
    }
    private var bank: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Sandbox only — not your live finances", systemImage: "building.columns")
            Text("Use the native bank setup, review the accounts shared by Plaid, then explicitly sync the accounts you want. Continuing here alone does not link or upload a bank.")
            Button("Set up Sandbox bank") { showingBanks = true }
                .buttonStyle(.borderedProminent).disabled(blocked).accessibilityIdentifier("onboarding.bank.connect")
        }
    }
    private var notifications: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Get a remote alert when a report is ready. The service decides when reports and alerts are due; this does not create local reminders or a device schedule.")
            if model.reportNotifications.capable {
                Button("Enable report alerts on this device") {
                    Task {
                        await controller.runDeviceAction {
                            await model.reportNotifications.refresh()
                            await model.reportNotifications.enable()
                            if model.reportNotifications.enabled { return "The service confirms report alerts are enabled for this installation." }
                            return model.reportNotifications.issue ?? "Permission setup was requested. APNs registration is not yet confirmed; continuing does not claim alerts are enabled."
                        }
                    }
                }.buttonStyle(.borderedProminent).disabled(blocked).accessibilityIdentifier("onboarding.notifications.enable")
            } else {
                Text("Remote report alerts are unavailable in this build or simulator. No local fallback is scheduled.")
                Button("Continue — alerts unavailable") { decide(.unavailable) }.disabled(blocked)
            }
            if let issue = model.reportNotifications.issue { Text(verbatim: issue).font(.callout) }
        }
    }
    private var decisions: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button("Continue to next step") { decide(.continue) }.buttonStyle(.bordered).disabled(blocked)
                .accessibilityIdentifier("onboarding.continue")
            Button("Skip this step") { decide(.skip) }.disabled(blocked).accessibilityIdentifier("onboarding.skip")
            Text("Continue records your setup choice, not device authorization. Skip does not start an upload or revoke a connection you already made.")
                .font(.footnote).foregroundStyle(.secondary)
        }
    }
    @ViewBuilder private func initialSync(_ value: OnboardingOverview) -> some View {
        Text("Your first sync").font(.title2.bold())
        Text("Only records actually received by the service can be confirmed. Health collection may still be running, and bank imports require an explicit sync. Empty or unavailable data is okay — you can finish now and retry later.")
        if value.decision(for: .health) == .continue {
            Button("Retry Health access and upload") {
                Task { await controller.runDeviceAction { await model.onboardingHealthUpload() } }
            }.disabled(blocked).accessibilityIdentifier("onboarding.sync.health")
        }
        if value.decision(for: .bank) == .continue {
            Button("Review and sync Sandbox accounts") { showingBanks = true }.disabled(blocked)
        }
        Text("No upload is started just because you skipped a step. Finishing checks available server records; it does not claim all device data has finished uploading.")
            .font(.callout).foregroundStyle(.secondary)
        Button("Check available records and finish") { decide(.finish) }
            .buttonStyle(.borderedProminent).disabled(blocked).accessibilityIdentifier("onboarding.finish")
        Button("Finish without waiting") { Task { await controller.finishWithoutWaiting() } }
            .disabled(controller.busy || model.busy).accessibilityIdentifier("onboarding.defer")
    }
    @ViewBuilder private func completion(_ summary: OnboardingInitialSync) -> some View {
        switch summary.status {
        case .records_available:
            Label("Records are available", systemImage: "checkmark.circle")
            if let count = summary.available_records { Text("The service confirmed \(count) stored records.") }
            Text("This does not confirm that every upload has finished or that every device permission is enabled.")
        case .no_data:
            Label("No records confirmed yet", systemImage: "tray")
            Text("The service found no available records at this check. Health access may be off, there may be no readable data, or an upload may still be pending. You can retry connections later in Settings.")
        case .deferred:
            Label("Data check deferred", systemImage: "clock")
            Text("Setup is saved, but a final record check was not confirmed. Uploads may still be pending. No successful or empty import is assumed; review connections later in Settings.")
        case .pending:
            Text("The service has not confirmed an initial data check.")
        }
        Button("Open overview") { controller.acknowledgeCompletion() }
            .buttonStyle(.borderedProminent).accessibilityIdentifier("onboarding.openOverview")
    }
    private func decide(_ action: OnboardingAction) { Task { await controller.decide(action) } }
}

/// Same native Link session and callback owner as the main BankingScreen, not a web fallback.
private struct OnboardingBankPresentation: View {
    @ObservedObject var controller: BankingController
    var body: some View {
        Color.clear.frame(width: 0, height: 0)
            .sheet(isPresented: $controller.showingLink, onDismiss: { controller.sheetDismissed() }) {
                if let session = controller.session { session.sheet() }
            }
    }
}
