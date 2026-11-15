import Foundation
import Combine

/// These are server decisions, never assertions about OS permission or readable data.
public enum OnboardingStepID: String, Codable, Sendable { case health, bank, notifications, initial_sync, complete }
public enum OnboardingDecision: String, Decodable, Sendable { case pending, `continue`, skip, denied, unavailable }
public enum OnboardingAction: String, Encodable, Sendable { case `continue`, skip, denied, unavailable, finish, `defer` }
public struct OnboardingStep: Decodable, Equatable, Sendable, Identifiable {
    public let id: OnboardingStepID
    public let title: String
    public let purpose: String
    public let optional: Bool
    public let decision: OnboardingDecision
}
public struct OnboardingInitialSync: Decodable, Equatable, Sendable {
    public enum Status: String, Decodable, Sendable { case pending, records_available, no_data, deferred }
    public let status: Status
    public let available_records: Int?
}
public struct OnboardingOverview: Decodable, Equatable, Sendable {
    public enum Status: String, Decodable, Sendable { case active, complete }
    public let schema_version: String
    public let data_epoch: String
    public let revision: Int
    public let status: Status
    public let current_step: OnboardingStepID
    public let legacy: Bool
    public let steps: [OnboardingStep]
    public let initial_sync: OnboardingInitialSync
    public var completed: Bool { status == .complete && current_step == .complete }
    public var current: OnboardingStep? { steps.first { $0.id == current_step } }
    public func decision(for step: OnboardingStepID) -> OnboardingDecision? { steps.first { $0.id == step }?.decision }
    public func validate() throws {
        guard schema_version == "1", !data_epoch.isEmpty, revision >= 0,
              steps.map(\.id) == [.health, .bank, .notifications], steps.allSatisfy(\.optional),
              (status == .complete) == (current_step == .complete),
              !legacy || completed, (initial_sync.available_records ?? 0) >= 0 else { throw ClientFailure.invalidResponse }
        if completed {
            guard initial_sync.status != .pending, legacy || steps.allSatisfy({ $0.decision != .pending }) else { throw ClientFailure.invalidResponse }
        }
        if status == .active {
            guard initial_sync.status == .pending else { throw ClientFailure.invalidResponse }
            let firstPending = steps.first { $0.decision == .pending }?.id ?? .initial_sync
            guard firstPending == current_step else { throw ClientFailure.invalidResponse }
        }
    }
}
public struct OnboardingActionRequest: Encodable, Equatable, Sendable {
    public let schema_version = "1"
    public let request_id: String
    public let data_epoch: String
    public let expected_revision: Int
    public let step: OnboardingStepID
    public let action: OnboardingAction
    public init(view: OnboardingOverview, action: OnboardingAction, requestID: String = UUID().uuidString) throws {
        try view.validate()
        guard !view.completed,
              (view.current_step == .initial_sync) == (action == .finish || action == .defer) else { throw ClientFailure.invalidResponse }
        request_id = requestID; data_epoch = view.data_epoch; expected_revision = view.revision
        step = view.current_step; self.action = action
    }
}

/// Memory-only progress. Backend persists decisions/receipts; Health retains its own durable outbox.
/// Retrying a lost response reuses the identical action, never a refreshed revision or epoch.
@MainActor public final class OnboardingController: ObservableObject {
    @Published public private(set) var overview: OnboardingOverview?
    @Published public private(set) var busy = false
    @Published public private(set) var issue: String?
    @Published public private(set) var deviceMessage: String?
    /// One in-memory acknowledgement for a finish initiated by THIS controller. Never persisted.
    @Published public private(set) var completionSummary: OnboardingInitialSync?
    private let client: CompanionClient
    private var generation = 0
    private var active = true
    private var pending: OnboardingActionRequest?
    private var completionRequestedHere = false
    private var revisionFloor: (epoch: String, revision: Int)?
    public var canShowMain: Bool { active && overview?.completed == true && completionSummary == nil && overview?.data_epoch == client.snapshot.state?.data_epoch }
    public var hasRetry: Bool { pending != nil }
    public init(client: CompanionClient) { self.client = client }
    public func suspend() {
        generation += 1; active = false; overview = nil; pending = nil
        busy = false; issue = nil; deviceMessage = nil; completionSummary = nil; revisionFloor = nil; completionRequestedHere = false
    }
    public func resume() { active = true }
    private func valid(_ token: Int, epoch: String? = nil) -> Bool {
        active && generation == token && !Task.isCancelled && (epoch == nil || client.snapshot.state?.data_epoch == epoch)
    }
    private func adopt(_ value: OnboardingOverview) throws {
        try value.validate()
        if let floor = revisionFloor, floor.epoch == value.data_epoch, floor.revision > value.revision { throw ClientFailure.invalidResponse }
        if let floor = revisionFloor, floor.epoch != value.data_epoch { completionSummary = nil; completionRequestedHere = false }
        revisionFloor = (value.data_epoch, value.revision)
        overview = value
    }
    public func load() async {
        guard active, !busy else { return }
        let token = generation; busy = true; issue = nil
        defer { if generation == token { busy = false } }
        do {
            try await client.refresh()
            guard valid(token) else { return }
            let value = try await client.refreshOnboarding()
            guard valid(token, epoch: value.data_epoch) else { return }
            // A fresh read reconciles an ambiguous result, it does not replay it.
            try adopt(value)
            if completionRequestedHere, value.completed, !value.legacy { completionSummary = value.initial_sync }
            if let pending, value.data_epoch != pending.data_epoch || value.revision > pending.expected_revision { self.pending = nil }
        } catch {
            guard valid(token) else { return }
            overview = nil
            issue = "Setup progress couldn’t be loaded. Your main overview stays locked until the service confirms it. Retry when connected."
        }
    }
    public func decide(_ action: OnboardingAction) async {
        guard active, !busy, pending == nil, let overview, !overview.completed else { return }
        do { pending = try OnboardingActionRequest(view: overview, action: action) }
        catch { issue = "Refresh setup before choosing again."; return }
        if overview.current_step == .initial_sync { completionRequestedHere = true }
        await retryDecision()
    }
    public func retryDecision() async {
        guard active, !busy, let request = pending else { return }
        let token = generation; busy = true; issue = nil
        defer { if generation == token { busy = false } }
        do {
            let value = try await client.onboarding(request)
            guard valid(token, epoch: request.data_epoch) else { return }
            try adopt(value)
            if request.step == .initial_sync, value.completed, !value.legacy { completionSummary = value.initial_sync }
            pending = nil; deviceMessage = nil
        } catch {
            guard valid(token) else { return }
            if case ClientFailure.rejected(let code, _) = error,
               ["stale_onboarding", "epoch_mismatch", "idempotency_conflict", "onboarding_complete", "invalid_request"].contains(code) {
                pending = nil; overview = nil
                issue = "Setup changed. Reload progress and review the current step; the old decision was not retagged."
            } else {
                issue = "This decision is not confirmed. Retry sends the same request, or reload to check server progress."
            }
        }
    }
    /// A failed availability check must not trap optional setup. Reconcile first;
    /// independent backend revision fencing makes a late finish and deferral mutually exclusive.
    public func finishWithoutWaiting() async {
        guard active, !busy, overview?.current_step == .initial_sync else { return }
        let token = generation
        await load()
        guard valid(token), let current = overview, current.current_step == .initial_sync else { return }
        pending = nil
        await decide(.defer)
    }
    public func acknowledgeCompletion() {
        guard active, overview?.completed == true, overview?.data_epoch == client.snapshot.state?.data_epoch else { return }
        completionSummary = nil; completionRequestedHere = false
    }
    /// Only a visible, explicit opt-in calls this. Device results never advance server progress.
    public func runDeviceAction(_ action: @MainActor () async -> String) async {
        guard active, !busy, pending == nil, let shown = overview, !shown.completed else { return }
        let token = generation; busy = true; issue = nil; deviceMessage = "Checking the selected device service…"
        defer { if generation == token { busy = false } }
        let result = await action()
        guard valid(token) else { return }
        guard valid(token, epoch: shown.data_epoch) else {
            overview = nil; deviceMessage = nil; pending = nil
            issue = "Your records changed. Reload setup before continuing."
            return
        }
        deviceMessage = result
    }
}
