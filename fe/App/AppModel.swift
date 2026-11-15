import Foundation
import SwiftUI
import Combine
import CompanionCore

/// Account-scoped native infrastructure. All Home/visit/answer authority comes from /experience/v1.
@MainActor final class AppModel: ObservableObject {
    @Published private(set) var snapshot = LocalSnapshot()
    @Published private(set) var experience: ExperienceV1?
    @Published private(set) var patternOverview: PatternOverview?
    @Published private(set) var patternBusy = false
    @Published private(set) var healthProgress: String?
    @Published private(set) var patternMessage: String?
    @Published private(set) var workflowOverview: WorkflowOverview?
    private var workflowPollTask: Task<Void, Never>?
    var patternRetryPending: Bool { patternRequestID != nil }
    private var patternRequestID: String?
    @Published private(set) var busy = false
    @Published var message: String?
    @Published private(set) var connected = false
    @Published private(set) var notificationPermission = "Not checked"
    /// Explicit dependency for legacy compatibility tests only; runtime uses banking v1.
    let plaid: PlaidController?
    let incrementalHealth: HealthIncrementalRuntime
    @Published private(set) var incrementalHealthMessage: String?
    let reportNotifications: ReportNotificationController
    private var notificationUpdates: AnyCancellable?
    let banking: BankingController
    private var bankingUpdates: AnyCancellable?
    private let client: CompanionClient
    private let health = HealthStepsAdapter()
    private let reminders: LocalReminders
    private var active = true
    private var admissionBlocked = false
    private var plaidUpdates: AnyCancellable?
    private var refreshTask: Task<Void, Never>?
    var state: CompanionCore.State? { active && !admissionBlocked && !snapshot.hidesCachedData ? snapshot.state : nil }
    var hasPending: Bool { snapshot.hasGeneralPendingChanges }

    init(client: CompanionClient, accountNamespace: String, legacyController: PlaidController? = nil) {
        self.client = client
        incrementalHealth = HealthIncrementalRuntime(client: client, account: accountNamespace)
        let notificationDevice = ReportNotificationAdapter()
        reportNotifications = ReportNotificationController(client: client, device: notificationDevice, accountNamespace: accountNamespace)
        notificationDevice.bind(reportNotifications)
        reminders = LocalReminders(accountNamespace: accountNamespace)
        // Do not publish disk snapshots before a current authenticated fetch succeeds.
        plaid = legacyController
        banking = BankingController(client: client)
        plaid?.stateChanged = { [weak self] in
            guard let self, self.active, !self.admissionBlocked else { return }
            self.publish()
            self.refreshTask?.cancel()
            self.refreshTask = Task { [weak self] in await self?.reloadExperience() }
        }
        banking.stateChanged = { [weak self] in
            guard let self, self.active, !self.admissionBlocked else { return }
            self.publish()
            self.refreshTask?.cancel()
            self.refreshTask = Task { [weak self] in await self?.reloadExperience() }
        }
        notificationUpdates = reportNotifications.objectWillChange.sink { [weak self] _ in Task { @MainActor in self?.objectWillChange.send() } }
        bankingUpdates = banking.objectWillChange.sink { [weak self] _ in Task { @MainActor in self?.objectWillChange.send() } }
        plaidUpdates = plaid?.objectWillChange.sink { [weak self] _ in Task { @MainActor in self?.objectWillChange.send() } }
    }
    func hideForAdmission() {
        incrementalHealth.stop()
        reportNotifications.suspend()
        admissionBlocked = true; connected = false; experience = nil; patternOverview = nil; patternRequestID = nil; workflowOverview = nil; workflowPollTask?.cancel(); snapshot = LocalSnapshot()
        health.cancelAll(); reminders.cancelAll(); plaid?.pauseForAdmission(); banking.pauseForAdmission()
    }
    func allowAdmittedUpdates() { reportNotifications.resume(); admissionBlocked = false; banking.admissionRestored() }
    func invalidateForSignOut(cancelReminders: Bool = true) {
        incrementalHealth.close()
        reportNotifications.suspend()
        active = false; banking.invalidateSession()
        refreshTask?.cancel(); refreshTask = nil
        plaid?.cancel("Signed out. Connection work stopped.")
        health.cancelAll()
        if cancelReminders { reminders.cancelAll() } else { reminders.suspendCallbacks() }
        client.invalidateSession()
        snapshot = LocalSnapshot(); experience = nil; patternOverview = nil; patternRequestID = nil; workflowOverview = nil; workflowPollTask?.cancel(); patternMessage = nil; message = nil; connected = false
    }
    func suspendForegroundForBackground() {
        reportNotifications.suspend(); admissionBlocked = true; connected = false
        health.cancelAll(); banking.pauseForAdmission(); plaid?.pauseForAdmission()
        workflowPollTask?.cancel(); refreshTask?.cancel(); refreshTask = nil
        reminders.suspendCallbacks(); snapshot = LocalSnapshot(); experience = nil; patternOverview = nil; workflowOverview = nil
    }
    private func publish() {
        guard active, !admissionBlocked else { return }
        let epoch = snapshot.state?.data_epoch
        snapshot = client.snapshot
        let olderExperience = experience.map { $0.data_epoch != snapshot.state?.data_epoch || $0.revision < (snapshot.state?.revision ?? 0) } ?? false
        if epoch != snapshot.state?.data_epoch || hasPending || olderExperience {
            experience = nil; reminders.cancelAll()
        }
        if epoch != nil && epoch != snapshot.state?.data_epoch {
            incrementalHealth.stop()
            reportNotifications.suspend(); reportNotifications.resume()
            patternOverview = nil; patternRequestID = nil; workflowOverview = nil; workflowPollTask?.cancel()
            plaid?.cancel("Records were reset. Reconnect explicitly if needed."); banking.cancelForLifecycle()
        }
    }
    private func reloadExperience() async {
        guard active, !admissionBlocked, !hasPending else { return }
        reportNotifications.resume()
        do {
            var value = try await client.refreshExperience()
            guard active, !admissionBlocked else { return }
            if plaid == nil, client.bankingSnapshot == nil, !banking.working {
                await banking.refresh(notify: false)
                guard active, !admissionBlocked else { return }
                publish(); value = try await client.refreshExperience()
            }
            guard active, !admissionBlocked else { return }
            experience = value; connected = true
            workflowOverview = try? await client.workflows()
            patternOverview = workflowOverview?.report
            pollWorkflow()
            guard active, !admissionBlocked else { patternOverview = nil; return }
            try await reminders.reconcile(state: state, locallyBlocked: hasPending)
            notificationPermission = reminders.permission
            await reportNotifications.refresh()
            await incrementalHealth.refresh(); incrementalHealthMessage = incrementalHealth.message
        } catch {
            guard active else { return }
            experience = nil; message = "Your overview couldn’t be loaded. Try refreshing."
        }
    }
    func workflowPage(_ id: String) -> WorkflowPage? { workflowOverview?.pages.first { $0.id == id } }
    func analyzePatterns() async {
        guard active, !admissionBlocked, !busy, !hasPending, workflowOverview?.action?.id == "enqueue" else { return }
        patternBusy = true; busy = true; patternMessage = nil
        defer { patternBusy = false; busy = false }
        if patternRequestID == nil { patternRequestID = UUID().uuidString }
        do {
            let value = try await client.workflows(requestID: patternRequestID)
            guard active, !admissionBlocked else { return }
            workflowOverview = value; patternOverview = value.report; patternRequestID = nil
            pollWorkflow()
        } catch {
            guard active, !admissionBlocked else { return }
            patternMessage = "Workflow status is unavailable. Retry keeps the same request."
        }
    }
    private func pollWorkflow() {
        workflowPollTask?.cancel()
        guard let status = workflowOverview?.job?.status, ["queued", "running"].contains(status) else { return }
        workflowPollTask = Task { [weak self] in
            for _ in 0..<120 {
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
                guard let self, self.active, !self.admissionBlocked, !Task.isCancelled else { return }
                do {
                    let value = try await self.client.workflows()
                    guard self.active, !self.admissionBlocked, !Task.isCancelled else { return }
                    self.workflowOverview = value; self.patternOverview = value.report
                    if !["queued", "running"].contains(value.job?.status ?? "") { return }
                } catch { return } // Offline or admission change: explicit refresh can resume status reads.
            }
        }
    }
    func clarifyPattern(_ concernID: String, attribution: String) async {
        guard active, !admissionBlocked, !busy, let run = patternOverview?.run_id else { return }
        busy = true; defer { busy = false }
        do {
            let value = try await client.clarifyPattern(runID: run, concernID: concernID, attribution: attribution)
            guard active, !admissionBlocked else { return }
            patternOverview = value
        } catch { patternMessage = "Context wasn’t saved. Refresh and try again." }
    }
    func savePatternBrief(_ text: String) async -> Bool {
        guard active, !admissionBlocked, !busy, let shown = patternOverview, let run = shown.run_id else { return false }
        busy = true; defer { busy = false }
        do {
            let value = try await client.savePatternBrief(runID: run, expectedBrief: shown.doctor_brief, text: text)
            guard active, !admissionBlocked else { return false }
            patternOverview = value; return true
        } catch { patternMessage = "The brief wasn’t saved. Refresh before editing again."; return false }
    }
    private func perform(preferReceivedExperience: Bool = false, _ action: (CompanionClient) async throws -> Void) async {
        guard active, !admissionBlocked, !busy else { return }
        busy = true; message = nil
        defer { busy = false }
        var diagnosticStage: LaunchDiagnostic.Stage = .synchronize
        do {
            try await action(client)
            guard active else { return }
            publish()
            if !hasPending, plaid == nil { await banking.refresh(notify: false); publish() }
            if !hasPending {
                if preferReceivedExperience, let received = client.experienceSnapshot { experience = received }
                else {
                    diagnosticStage = .experienceRequest
                    experience = try await client.refreshExperience()
                }
                let patterns = try? await client.patterns()
                guard active, !admissionBlocked else { return }
                patternOverview = patterns
            }
            guard active else { return }
            connected = true
            diagnosticStage = .reminders
            try await reminders.reconcile(state: state, locallyBlocked: hasPending)
            notificationPermission = reminders.permission
            await reportNotifications.refresh()
            await incrementalHealth.refresh(); incrementalHealthMessage = incrementalHealth.message
        } catch {
            LaunchDiagnostic.failure(error, at: diagnosticStage)
            guard active else { return }
            publish(); experience = nil; connected = false; reminders.cancelAll()
            if case ClientFailure.rejected(let code, _) = error, ["stale_experience", "obsolete_pattern"].contains(code) {
                do {
                    try client.discardObsoleteAnswers(); try await client.refresh()
                    let current = try await client.refreshExperience()
                    guard active else { return }
                    publish(); experience = current; connected = true
                } catch {}
                message = "This question changed. Review the current question before answering again."
            } else if case AuthenticationFailure.unavailable = error {
                message = "The secure service is temporarily unavailable. Try again without changing accounts."
            } else if error is AuthenticationFailure {
                experience = nil; snapshot = LocalSnapshot()
                message = "Sign in again to access your records."
            } else if error is URLError {
                message = "We couldn’t reach your service. Your unsent changes remain saved for this account."
            } else if case ClientFailure.pendingChanges = error {
                message = "Some changes are waiting to save. Review them in Settings before making another change."
            } else { message = "That action couldn’t finish safely. Refresh and try again." }
        }
    }
    func refresh() async {
        guard active, !admissionBlocked else { return }
        // Retry the unchanged Health journal through its own admission before a stale
        // foreground outbox error can hide the readiness/recovery path.
        await incrementalHealth.refresh(); incrementalHealthMessage = incrementalHealth.message
        await LaunchDiagnostic.$enabled.withValue(true) {
            await perform { try await $0.synchronize() }
        }
    }
    func discardPending() async { await perform { try $0.discardPending(); try await $0.refresh() } }
    private func submit<T: Encodable>(_ value: T, path: String, method: String = "POST", replacingQueue: Bool = false, client: CompanionClient) async throws {
        guard active else { throw AuthenticationFailure.locked }
        try client.enqueue(PendingMutation(path: path, method: method, request: value), replacingQueue: replacingQueue)
        publish(); reminders.cancelAll(); try await client.synchronize()
    }
    func answerExperience(_ answer: String) async {
        guard let shown = experience, let question = shown.clarification,
              question.can_answer, question.options.contains(where: { $0.value == answer }) else { return }
        let request = ExperienceAnswerRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: shown.data_epoch,
            expected_revision: shown.revision, clarification_key: question.key, answer: answer)
        await perform(preferReceivedExperience: true) { client in try await submit(request, path: "/experience/v1/answers", client: client) }
    }
    func deleteData() async {
        incrementalHealth.stop()
        reportNotifications.stopForLogout()
        await reportNotifications.clearDeviceAlerts()
        banking.cancelForLifecycle(); plaid?.cancel("Deletion requested. Connection work stopped."); health.cancelAll(); reminders.cancelAll(); experience = nil; patternOverview = nil; patternRequestID = nil; workflowOverview = nil; workflowPollTask?.cancel()
        await perform { client in
            guard let current = client.snapshot.state else { throw ClientFailure.noState }
            try await submit(DeleteRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: current.data_epoch, confirm: "delete_all"), path: "/data", method: "DELETE", replacingQueue: true, client: client)
        }
    }
    func prepareNotificationLogout() async throws {
        incrementalHealth.close()
        health.cancelAll(); banking.cancelForLifecycle(); workflowPollTask?.cancel()
        try await reportNotifications.logout()
    }
    func process() async { await process(mode: "disabled") }
    func processLive() async { await process(mode: "live") }
    private func process(mode: String) async {
        await perform { client in
            guard !client.snapshot.hasGeneralPendingChanges, let current = client.snapshot.state else { throw ClientFailure.pendingChanges }
            if mode == "live" && (!current.preferences.cloud_consent || current.observations.contains(where: { $0.mode == "live" })) { throw ClientFailure.rejected("consent_required", false) }
            try await submit(ProcessRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: current.data_epoch, synthesis_mode: mode), path: "/process", client: client)
        }
    }
    func feedback(cardID: String, kind: String, value: String, scope: String = "card", event: EvidenceRef? = nil, attribution: String? = nil, note: String = "", expectedEpoch: String? = nil, expectedCardRevision: Int? = nil) async {
        await perform { client in
            guard let current = client.snapshot.state else { throw ClientFailure.noState }
            if let expectedEpoch, expectedEpoch != current.data_epoch { throw ClientFailure.rejected("stale_feedback_form", false) }
            if let expectedCardRevision, current.ledger.first(where: { $0.id == cardID })?.revision != expectedCardRevision { throw ClientFailure.rejected("stale_feedback_form", false) }
            try await submit(FeedbackRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: current.data_epoch, card_id: cardID, kind: kind, value: value, scope: scope, event_ref: event, attribution: attribution, note: String(note.prefix(1000))), path: "/feedback", client: client)
        }
    }
    func savePreferences(_ preferences: Preferences, epoch: String, revision: Int) async {
        await perform { client in
            guard !client.snapshot.hasGeneralPendingChanges else { throw ClientFailure.pendingChanges }
            try await client.refresh()
            guard let current = client.snapshot.state, current.data_epoch == epoch, current.revision == revision else { throw ClientFailure.rejected("stale_preference_form", false) }
            try await submit(PreferencesRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: epoch, preferences: preferences), path: "/preferences", method: "PUT", client: client)
        }
    }
    func healthSync(connect: Bool) async {
        await perform { client in
            guard !client.snapshot.hasGeneralPendingChanges else { throw ClientFailure.pendingChanges }
            try await client.refresh()
            guard var current = client.snapshot.state else { throw ClientFailure.noState }
            if connect {
                var preferences = current.preferences
                if !preferences.enabled_source_ids.contains("healthkit") { preferences.enabled_source_ids.append("healthkit") }
                try await submit(PreferencesRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: current.data_epoch, preferences: preferences), path: "/preferences", method: "PUT", client: client)
                guard active, let refreshed = client.snapshot.state else { throw AuthenticationFailure.locked }; current = refreshed
            }
            guard current.sources.contains(where: { $0.id == "healthkit" && $0.enabled && $0.consent == "granted" }) else { throw ClientFailure.rejected("consent_required", false) }
            await incrementalHealth.refresh()
            if incrementalHealth.blocksSnapshot || client.incrementalHealthHasState {
                try await incrementalHealth.connect()
                incrementalHealthMessage = incrementalHealth.message
                return
            }
            healthProgress = "Reading up to six months of permitted Health records…"
            defer { healthProgress = nil }
            let pages = try await health.collectNormalized(state: current, requestAuthorization: true)
            guard active, !admissionBlocked, client.snapshot.state?.data_epoch == current.data_epoch else { throw AuthenticationFailure.locked }
            // Enqueue the complete collection BEFORE sending, so an offline interruption cannot strand a lost final page.
            for page in pages { try client.enqueue(PendingMutation(path: "/health/v1/samples", request: page)) }
            publish()
            healthProgress = "Saving \(pages.reduce(0) { $0 + $1.samples.count }) records in \(pages.count) private batches…"
            try await client.synchronize()
            if pages.allSatisfy({ $0.samples.isEmpty }) { message = "No supported Health samples were readable. Read permission may be off or records may be absent; nothing was erased." }
        }
    }
    func requestNotifications() async {
        guard active else { return }
        do { try await reminders.requestPermission(); guard active else { reminders.cancelAll(); return }; notificationPermission = reminders.permission }
        catch { message = "Reminder permission couldn’t be checked." }
    }
    private func updatePreferences(_ edit: (inout Preferences) -> Void) async {
        await perform { client in
            guard !client.snapshot.hasGeneralPendingChanges else { throw ClientFailure.pendingChanges }
            try await client.refresh(); guard let current = client.snapshot.state else { throw ClientFailure.noState }
            var preferences = current.preferences; edit(&preferences)
            try await submit(PreferencesRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: current.data_epoch, preferences: preferences), path: "/preferences", method: "PUT", client: client)
        }
    }
    func disconnectHealth() async {
        incrementalHealth.stop(); health.cancelAll()
        do { try client.setHealthCollectionPaused(true) } catch { message = "Device pause could not be saved. Retry after unlocking."; return }
        await updatePreferences { $0.enabled_source_ids.removeAll { $0 == "healthkit" } } }
    func setCloudConsent(_ enabled: Bool) async { await updatePreferences { $0.cloud_consent = enabled } }
    func enableReminders() async { await requestNotifications(); guard active, ["Authorized", "Provisional", "Temporary"].contains(notificationPermission) else { return }; await updatePreferences { $0.notifications.enabled = true } }
    func stopReminders() async { reminders.cancelAll(); await updatePreferences { $0.notifications.enabled = false } }

    var bankHasConsent: Bool { state?.sources.contains { $0.id == "plaid" && $0.consent == "granted" } ?? false }
    var bankCanDisconnect: Bool { bankHasConsent || (state?.sources.contains { $0.id == "plaid" && $0.status == "error" } ?? false) }
    var healthHasConsent: Bool { state?.sources.contains { $0.id == "healthkit" && $0.enabled && $0.consent == "granted" } ?? false }
    var bankConnection: ExperienceConnection {
        let source = state?.sources.first { $0.id == "plaid" }
        let status: ExperienceConnectionState = plaid?.working == true ? .working : (source?.status == "error" || plaid?.lastIssue != nil) ? .needsAttention : !bankHasConsent ? .notConnected : !connected ? .accessNotConfirmed : (source?.status == "ready" || source?.status == "syncing" || source?.last_success_at != nil) ? .connected : .accessNotConfirmed
        return ExperienceConnection(kind: .bank, state: status, isSandbox: true, error: plaid?.lastIssue)
    }
    var healthConnection: ExperienceConnection {
        let source = state?.sources.first { $0.id == "healthkit" }
        let status: ExperienceConnectionState = !healthHasConsent ? .notConnected : source?.status == "error" ? .needsAttention : (!connected || source?.read_access != "samples_readable") ? .accessNotConfirmed : .connected
        return ExperienceConnection(kind: .appleHealth, state: status)
    }
    var reminderConnection: ExperienceConnection {
        ExperienceConnection(kind: .reminders, state: ["Authorized", "Provisional", "Temporary"].contains(notificationPermission) ? .connected : .notConnected, error: notificationPermission == "Denied in iOS Settings" ? "Reminders are off in iOS Settings." : nil)
    }
}
