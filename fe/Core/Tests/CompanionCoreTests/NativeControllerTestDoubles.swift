#if os(macOS)
import Foundation
import CompanionCore

// Test-target-only substitutes. Real AppModel/PlaidController sources are compiled via symlinks.
// No network, SDK, permissions, notifications, keychain or simulator can be reached here.
struct LinkSuccess { let publicToken: String }
struct LinkExit { let error: Error? }
struct LinkTokenConfiguration {
    let token: String
    let onSuccess: (LinkSuccess) -> Void
    let onExit: (LinkExit) -> Void
    let onEvent: ((String) -> Void)?
    let onLoad: () -> Void
}
@MainActor final class PlaidLinkSession {
    let configuration: LinkTokenConfiguration
    init(_ configuration: LinkTokenConfiguration) { self.configuration = configuration }
}
@MainActor enum Plaid {
    static func createPlaidLinkSession(configuration: LinkTokenConfiguration) throws -> PlaidLinkSession {
        PlaidLinkSession(configuration)
    }
}
@MainActor final class HealthStepsAdapter {
    func cancelAll() {}
    func collect(state: CompanionCore.State, requestAuthorization: Bool) async throws -> ObservationBatch {
        throw NativeTestUnexpectedCall.permission
    }
}
private enum NativeTestUnexpectedCall: Error { case permission }
@MainActor final class LocalReminders {
    var permission = "Not checked"
    init(accountNamespace: String) {}
    func cancelAll() {}
    func suspendCallbacks() {}
    func reconcile(state: CompanionCore.State?, locallyBlocked: Bool) async throws {}
    func requestPermission() async throws { throw NativeTestUnexpectedCall.permission }
}
enum BankingViewStatus: Equatable { case idle, loading, updating, failed(String) }
enum ExperienceConnectionKind { case bank, appleHealth, reminders }
enum ExperienceConnectionState { case notConnected, working, connected, accessNotConfirmed, unavailable, needsAttention }
struct ExperienceConnection {
    let kind: ExperienceConnectionKind
    let state: ExperienceConnectionState
    var isSandbox = false
    var error: String? = nil
}
#endif
