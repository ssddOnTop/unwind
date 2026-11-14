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
    private let fence = AuthSessionFence()
    private let apiOrigin: URL?
    private var provider: ClerkTokenProvider?
    private var bearer: AuthenticatedTransport?
    private var cache: AccountCache?
    private var work: Task<Void, Never>?
    private var observation: AnyCancellable?
    private var suspended = false
    private var managingAccount = false
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
    var canShowAccountData: Bool { ready && bearer?.isAdmitted == true && !locallyLocked && sdkIdentity != nil && sdkIdentity == preparedIdentity }
    private func cancelLocalReminders() {
        let center = UNUserNotificationCenter.current()
        center.removeAllPendingNotificationRequests(); center.removeAllDeliveredNotifications()
    }
    private func clearLocally(persistLock: Bool) {
        ready = false; cancelLocalReminders()
        if persistLock { locallyLocked = true; defaults.set(true, forKey: lockKey) }
        work?.cancel(); work = nil; bearer?.cancel(); bearer = nil
        model?.invalidateForSignOut(); model = nil; observation = nil; preparedIdentity = nil
        do { try cache?.clearActive() }
        catch { locallyLocked = true; defaults.set(true, forKey: lockKey); issue = "Local data could not be cleared. Unlock your device and retry sign-out." }
    }
    func observeSDK() {
        guard !suspended, clerk?.isLoaded == true else { return }
        guard let identity = sdkIdentity else {
            fence.lock(); return
        }
        if managingAccount && managedSession != identity.sessionID {
            managingAccount = false; activateDeliberately(); return
        }
        guard !locallyLocked else { return }
        if identity != preparedIdentity { prepare(identity) }
    }
    func authCompleted() { managingAccount = false; activateDeliberately() }
    func activateDeliberately() {
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
        guard let origin = apiOrigin, let provider, let cache else { return }
        if let previous = preparedIdentity, previous != identity { fence.lock() }
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
                let instance = AppModel(client: client, accountNamespace: identity.accountNamespace)
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
        suspended = true; ready = false
        fence.suspend(); work?.cancel(); work = nil
        model?.invalidateForSignOut(cancelReminders: false); model = nil; observation = nil
        bearer?.cancel(); bearer = nil; preparedIdentity = nil
    }
    func resumeFromBackground() {
        guard suspended else { return }
        suspended = false; cancelLocalReminders(); observeSDK()
    }
    func prepareAccountManagement() {
        managedSession = sdkIdentity?.sessionID; managingAccount = true
        fence.lock() // synchronous local purge BEFORE any native account switch/sign-out UI
    }
    func signOut() {
        managingAccount = false
        fence.lock()
        work = Task { [weak self] in
            guard let self, let clerk else { return }
            do { try await clerk.auth.signOut() }
            catch { self.issue = "Signed out locally. Clerk sign-out could not be confirmed; your records remain locked. Retry sign-out." }
        }
    }
    func retryService() {
        guard !locallyLocked, let identity = sdkIdentity else { return }
        prepare(identity)
    }
    func retrySDK() async {
        guard let clerk else { return }
        do { _ = try await clerk.refreshEnvironment(); _ = try await clerk.refreshClient(); observeSDK() }
        catch { issue = "Clerk could not load. Check connectivity and that Native API is enabled for this Clerk instance." }
    }
}
