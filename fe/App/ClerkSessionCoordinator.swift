import Foundation
import SwiftUI
import Combine
import UserNotifications
import ClerkKit
import CompanionCore

struct PublicAuthConfiguration {
    let publishableKey: String
    let issuer: String
    init(bundle: Bundle = .main) throws {
        guard let key = bundle.object(forInfoDictionaryKey: "ClerkPublishableKey") as? String,
              key.hasPrefix("pk_test_") || key.hasPrefix("pk_live_"), !key.contains("$("),
              let encoded = bundle.object(forInfoDictionaryKey: "ClerkIssuerBase64") as? String,
              let data = Data(base64Encoded: encoded), let issuer = String(data: data, encoding: .utf8),
              let url = URLComponents(string: issuer), url.scheme == "https", url.host != nil,
              url.path.isEmpty, url.query == nil, url.fragment == nil, url.user == nil, url.password == nil else { throw AuthenticationFailure.unavailable }
        self.publishableKey = key; self.issuer = issuer
    }
}

@MainActor final class ClerkTokenProvider: SessionTokenProvider {
    let clerk: Clerk
    let issuer: String
    init(clerk: Clerk, issuer: String) { self.clerk = clerk; self.issuer = issuer }
    var currentIdentity: AuthIdentity? {
        guard clerk.isLoaded, let session = clerk.session, session.status == .active,
              let user = clerk.user, session.user?.id == user.id else { return nil }
        return AuthIdentity(issuer: issuer, subject: user.id, sessionID: session.id)
    }
    func token(for identity: AuthIdentity, forceRefresh: Bool) async throws -> String? {
        guard currentIdentity == identity, let session = clerk.session else { throw AuthenticationFailure.changedSession }
        let value = try await session.getToken(.init(skipCache: forceRefresh))
        guard currentIdentity == identity else { throw AuthenticationFailure.changedSession }
        return value
    }
}

@MainActor final class ClerkSessionCoordinator: ObservableObject {
    let clerk: Clerk?
    let configuration: PublicAuthConfiguration?
    @Published private(set) var model: AppModel?
    @Published private(set) var ready = false
    @Published private(set) var locallyLocked: Bool
    @Published private(set) var issue: String?
    private let countPreflight: QueueCountPreflight? = QueueCountPreflight.requested ? QueueCountPreflight() : nil
    var isCountPreflight: Bool { countPreflight != nil }
    private let fence = AuthSessionFence()
    private let apiOrigin: URL?
    private var provider: ClerkTokenProvider?
    private var bearer: AuthenticatedTransport?
    private var cache: AccountCache?
    private var work: Task<Void, Never>?
    private var observation: AnyCancellable?
    private var suspended = false
    private var managingAccount = false
    private var endingSession = false
    private var managedSession: String?
    private var preparedIdentity: AuthIdentity?
    private let defaults = UserDefaults.standard
    private let lockKey = "authenticated.local-lock.v1"

    init() {
        apiOrigin = try? APIConfiguration.resolve()
        locallyLocked = UserDefaults.standard.bool(forKey: "authenticated.local-lock.v1")
        configuration = try? PublicAuthConfiguration()
        guard let apiOrigin, apiOrigin.host != configuration.flatMap({ URL(string: $0.issuer)?.host?.lowercased() }) else {
            clerk = nil
            issue = "API address is invalid. Edit APIConfiguration.baseURL to an HTTPS root origin (HTTP is allowed only for localhost or 127.0.0.1), separate from Clerk, then rebuild. Paths, credentials, queries and fragments are not supported."
            cancelLocalReminders()
            return
        }
        if let configuration {
            let clerk = Clerk.configure(publishableKey: configuration.publishableKey, options: .init(logLevel: .error, telemetryEnabled: false))
            self.clerk = clerk; provider = ClerkTokenProvider(clerk: clerk, issuer: configuration.issuer)
        } else { clerk = nil; issue = "Sign-in is not configured. Generate the approved public Clerk configuration and rebuild." }
        if isCountPreflight {
            // No directory creation, marker activation, legacy deletion or local-lock writes.
            if let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
                cache = AccountCache(root: support.appendingPathComponent("AuthenticatedAccounts", isDirectory: true), deployment: apiOrigin.absoluteString)
            }
            return
        }
        do {
            let support = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            cache = AccountCache(root: support.appendingPathComponent("AuthenticatedAccounts", isDirectory: true), deployment: apiOrigin.absoluteString)
            // Never adopt the previous anonymous app's files or locally authoritative answers.
            let legacy = support.appendingPathComponent("LocalCompanion", isDirectory: true)
            if FileManager.default.fileExists(atPath: legacy.path) { try FileManager.default.removeItem(at: legacy) }
            if locallyLocked { try cache?.clearActive() }
        } catch { locallyLocked = true; defaults.set(true, forKey: lockKey); issue = "Protected local storage is unavailable. Unlock the device before signing in." }
        cancelLocalReminders()
        fence.onLock = { [weak self] in self?.clearLocally(persistLock: true) }
    }
    var sdkIdentity: AuthIdentity? { provider?.currentIdentity }
    var canShowAccountData: Bool { !isCountPreflight && ready && bearer?.isAdmitted == true && !locallyLocked && sdkIdentity != nil && sdkIdentity == preparedIdentity }
    private func cancelLocalReminders() {
        guard !isCountPreflight else { return }
        let center = UNUserNotificationCenter.current()
        center.removeAllPendingNotificationRequests(); center.removeAllDeliveredNotifications()
    }
    private func clearLocally(persistLock: Bool) {
        guard !isCountPreflight else { return }
        ready = false; cancelLocalReminders()
        if persistLock { locallyLocked = true; defaults.set(true, forKey: lockKey) }
        work?.cancel(); work = nil; bearer?.cancel(); bearer = nil
        model?.invalidateForSignOut(); model = nil; observation = nil; preparedIdentity = nil
        do { try cache?.clearActive() }
        catch { locallyLocked = true; defaults.set(true, forKey: lockKey); issue = "Local data could not be cleared. Unlock your device and retry sign-out." }
    }
    func observeSDK() {
        if isCountPreflight {
            guard clerk?.isLoaded == true else { return }
            runCountPreflight(); return
        }
        if suspended {
            if sdkIdentity != preparedIdentity { model?.incrementalHealth.close() }
            return
        }
        guard clerk?.isLoaded == true else { return }
        guard let identity = sdkIdentity else {
            model?.reportNotifications.stopForLogout()
            fence.lock(); return
        }
        if managingAccount && managedSession != identity.sessionID {
            managingAccount = false; activateDeliberately(); return
        }
        guard !locallyLocked else { return }
        if identity != preparedIdentity { prepare(identity) }
    }
    func authCompleted() { guard !isCountPreflight else { return }; managingAccount = false; activateDeliberately() }
    func activateDeliberately() {
        if isCountPreflight { runCountPreflight(); return }
        guard let identity = sdkIdentity, cache != nil else { return }
        do { try cache?.clearActive() }
        catch {
            locallyLocked = true; defaults.set(true, forKey: lockKey)
            issue = "Old local data could not be cleared. Unlock your device and retry before using this account."
            return
        }
        locallyLocked = false; defaults.set(false, forKey: lockKey); issue = nil
        prepare(identity)
    }
    private func prepare(_ identity: AuthIdentity) {
        guard !isCountPreflight else { return }
        guard let origin = apiOrigin, let provider, let cache else { return }
        if let previous = preparedIdentity, previous != identity { model?.reportNotifications.stopForLogout(); fence.lock() }
        // Retry/foreground of the same account keeps queued bytes on disk, but destroys old objects.
        work?.cancel(); model?.invalidateForSignOut(); model = nil; observation = nil
        bearer?.cancel(); bearer = nil; ready = false; issue = nil
        locallyLocked = false; defaults.set(false, forKey: lockKey)
        let lease = fence.activate(identity); preparedIdentity = identity
        work = Task { [weak self] in
            guard let self else { return }
            do {
                let transport = try AuthenticatedTransport(base: URLSessionTransport(), provider: provider, fence: self.fence, lease: lease, origin: origin)
                self.bearer = transport
                transport.onAdmissionInvalidated = { [weak self] in
                    self?.ready = false; self?.model?.hideForAdmission()
                }
                transport.onAdmissionRestored = { [weak self] in self?.model?.allowAdmittedUpdates() }
                try await transport.admit()
                try self.fence.validate(lease)
                guard self.sdkIdentity == identity, !Task.isCancelled else { return }
                // The network-verified principal gate MUST precede opening any private cache.
                let storage = try cache.activate(identity)
                let client = try CompanionClient(storage: storage, transport: transport, baseURL: origin, sessionValidator: { try transport.validate() })
                let healthLease = try HealthBackgroundLease(identity: identity, provider: provider, base: URLSessionTransport(), origin: origin)
                let instance = AppModel(client: client, accountNamespace: identity.accountNamespace)
                instance.incrementalHealth.configure(healthLease)
                self.model = instance
                self.observation = instance.objectWillChange.sink { [weak self, weak instance] _ in
                    Task { @MainActor in
                        guard let self, let instance, self.model === instance, !self.locallyLocked,
                              self.sdkIdentity == identity, self.bearer === transport else { return }
                        if !transport.isAdmitted { self.ready = false }
                        else if !self.ready, instance.experience != nil, instance.connected { self.ready = true }
                        self.objectWillChange.send()
                    }
                }
                await instance.refresh()
                guard self.model === instance, (try? self.fence.validate(lease)) != nil, self.sdkIdentity == identity else { return }
                self.ready = transport.isAdmitted && instance.experience != nil && instance.connected
                if !self.ready { self.issue = instance.message ?? "The secure overview is unavailable. Try again." }
            } catch {
                guard (try? self.fence.validate(lease)) != nil, self.sdkIdentity == identity, !Task.isCancelled else { return }
                self.ready = false
                if error as? AuthenticationFailure == .profileMismatch {
                    self.issue = "This server is not a compatible authenticated backend for this account. Check the API endpoint and Clerk configuration."
                } else { self.issue = "Secure access could not be verified. Check the authenticated service and try again. Your records remain locked." }
            }
        }
    }
    func suspendForBackground() {
        if let countPreflight { countPreflight.stop(); work?.cancel(); bearer?.cancel(); return }
        suspended = true; ready = false
        bearer?.onAdmissionInvalidated = nil; bearer?.onAdmissionRestored = nil
        fence.suspend(); work?.cancel(); work = nil
        model?.suspendForegroundForBackground(); observation = nil
        bearer?.cancel(); bearer = nil
    }
    func resumeFromBackground() {
        guard !isCountPreflight else { return }
        guard suspended else { return }
        suspended = false; cancelLocalReminders()
        if let identity = sdkIdentity { prepare(identity) } else { fence.lock() }
    }
    private func runCountPreflight() {
        guard let countPreflight, !countPreflight.attempted, work == nil else { return }
        guard let identity = sdkIdentity, let origin = apiOrigin, let provider, let cache else {
            countPreflight.stop(); issue = "Count preflight unavailable. No application work was sent."; return
        }
        let lease = fence.activate(identity)
        work = Task { [weak self] in
            guard let self else { return }
            await countPreflight.run(admit: {
                let transport = try AuthenticatedTransport(base: URLSessionTransport(), provider: provider, fence: self.fence, lease: lease, origin: origin)
                self.bearer = transport
                // Ordinary Clerk verification/token refresh and the existing GET handshake only.
                try await transport.admit()
                try self.fence.validate(lease)
                guard self.sdkIdentity == identity else { throw AuthenticationFailure.changedSession }
            }, read: {
                guard let bearer = self.bearer else { throw AuthenticationFailure.notAdmitted }
                try bearer.validate()
                try self.fence.validate(lease)
                guard self.sdkIdentity == identity else { throw AuthenticationFailure.changedSession }
                let snapshot = try cache.preflightSnapshot(identity)
                guard let epoch = snapshot.state?.data_epoch else { throw AuthenticationFailure.unavailable }
                let revocations = try ReportNotificationController.preflightPendingRevocations(accountNamespace: identity.accountNamespace, epoch: epoch)
                return QueueCountPreflight.Counts(snapshot: snapshot, notificationRevocations: revocations)
            })
            self.bearer?.cancel(); self.bearer = nil
            self.issue = countPreflight.result ?? "queue-preflight-v1 status=unavailable"
        }
    }
    private func revokeCapturedNotificationSession(_ identity: AuthIdentity) async throws {
        guard let origin = apiOrigin, let provider, provider.currentIdentity == identity else { throw AuthenticationFailure.changedSession }
        let logoutFence = AuthSessionFence()
        // Use one isolated fence, never revive the main UI/cache admission.
        let captured = logoutFence.activate(identity)
        let transport = try AuthenticatedTransport(base: URLSessionTransport(), provider: provider, fence: logoutFence, lease: captured, origin: origin)
        defer { transport.cancel(); logoutFence.lock() }
        try await transport.admit()
        try await ReportNotificationSessionLogout.perform(transport: transport, origin: origin)
    }
    func prepareAccountManagement() async -> Bool {
        guard !isCountPreflight else { return false }
        guard !endingSession else { return false }
        endingSession = true; defer { endingSession = false }
        let identity = sdkIdentity, notifications = model?.reportNotifications
        model?.incrementalHealth.close()
        notifications?.stopForLogout()
        fence.lock() // immediate local purge; isolated RPC retains only captured identity authority
        do {
            await ReportNotificationAdapter().clearDelivered()
            if let identity { try await revokeCapturedNotificationSession(identity); notifications?.confirmLogout() }
            else if clerk?.session != nil { throw AuthenticationFailure.unavailable }
            guard sdkIdentity == identity else { throw AuthenticationFailure.changedSession }
            managedSession = identity?.sessionID; managingAccount = true
            return true
        } catch {
            issue = "Local records are locked. Server report-alert revocation is unconfirmed; reconnect and retry Manage sign-in."
            return false
        }
    }
    func signOut() {
        guard !isCountPreflight else { return }
        guard !endingSession else { return }
        endingSession = true; managingAccount = false
        let identity = sdkIdentity, notifications = model?.reportNotifications
        model?.incrementalHealth.close()
        notifications?.stopForLogout()
        fence.lock()
        work = Task { [weak self] in
            guard let self, let clerk else { return }
            defer { self.endingSession = false }
            do {
                await ReportNotificationAdapter().clearDelivered()
                if let identity { try await self.revokeCapturedNotificationSession(identity); notifications?.confirmLogout() }
                else if clerk.session != nil { throw AuthenticationFailure.unavailable }
                guard self.sdkIdentity == identity else { throw AuthenticationFailure.changedSession }
                try await clerk.auth.signOut()
            } catch { self.issue = "Local records are locked. Server report-alert revocation or SDK sign-out is unconfirmed. Reconnect and retry Sign out before switching accounts." }
        }
    }
    func retryService() {
        guard !isCountPreflight else { return }
        guard !locallyLocked, let identity = sdkIdentity else { return }
        prepare(identity)
    }
    func retrySDK() async {
        guard !isCountPreflight else { return }
        guard let clerk else { return }
        do { _ = try await clerk.refreshEnvironment(); _ = try await clerk.refreshClient(); observeSDK() }
        catch { issue = "Clerk could not load. Check connectivity and that Native API is enabled for this Clerk instance." }
    }
}
