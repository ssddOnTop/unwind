import Foundation
import Combine

@MainActor public protocol ReportNotificationDevice: AnyObject {
    var capable: Bool { get }
    func authorized() async -> Bool
    func requestPermission() async throws -> Bool
    func register()
    func unregister()
    func clearDelivered() async
}

/// Device infrastructure only. Backend decides eligibility, due times and delivery.
/// This cache contains opaque admission metadata, never a device token or report.
@MainActor public final class ReportNotificationController: ObservableObject {
    @Published public private(set) var status: NotificationStatus?
    @Published public private(set) var issue: String?
    @Published public private(set) var busy = false
    private struct Cache: Codable {
        var requestID: String
        var epoch: String
        var binding: String
        var installationID: String?
        var pendingRevoke = false
        var revokeInstallationID: String?
        var revokeConsentRevision: Int?
    }
    private let client: CompanionClient
    private let device: any ReportNotificationDevice
    private let defaults: UserDefaults
    private let cacheKey: String
    private var cache: Cache?
    private var generation = 0
    private var active = true
    private var registration: NotificationRegister?
    public var capable: Bool { device.capable }
    public var installation: NotificationInstallation? {
        guard let id = cache?.installationID else { return nil }
        return status?.installations.first { $0.id == id }
    }
    public var enabled: Bool { installation?.enabled == true }
    public init(client: CompanionClient, device: any ReportNotificationDevice, accountNamespace: String, defaults: UserDefaults = .standard) {
        self.client = client; self.device = device; self.defaults = defaults
        cacheKey = "report-notification-admission.v1." + accountNamespace
        if let data = defaults.data(forKey: cacheKey) { cache = try? JSONDecoder().decode(Cache.self, from: data) }
    }
    private func save() {
        if let cache, let data = try? JSONEncoder().encode(cache) { defaults.set(data, forKey: cacheKey) }
        else { defaults.removeObject(forKey: cacheKey) }
    }
    private func adopt(_ value: NotificationStatus) {
        if let old = cache, old.epoch != value.data_epoch || old.binding != value.session_binding { cache = nil }
        status = value
        if let id = value.selected_installation_id { cache?.installationID = id }
        save()
    }
    public func suspend() {
        generation += 1; active = false; registration = nil; status = nil
    }
    public func resume() { active = true }
    public func refresh() async {
        guard active, !busy else { return }
        busy = true; defer { busy = false }
        let captured = generation
        do {
            let value = try await client.reportNotificationStatus(registrationRequestID: cache?.requestID)
            guard active, captured == generation else { return }
            adopt(value)
            if cache?.pendingRevoke == true { try await flushRevoke(); return }
            guard device.capable else { issue = "Remote report alerts are unavailable in this build. No local fallback is scheduled."; return }
            guard enabled, value.delivery_status == "ready", !value.session_revoked else { return }
            let authorized = await device.authorized()
            guard active, captured == generation else { return }
            if !authorized { markRevoke(); try await flushRevoke(); return }
            registration = makeRegistration(optIn: false)
            if registration != nil { device.register() }
        } catch { if active, captured == generation { issue = "Report notification status is unavailable. No registration or revocation is assumed." } }
    }
    private func makeRegistration(optIn: Bool) -> NotificationRegister? {
        guard let status, let revision = status.scope_revision, let cache else { return nil }
        return NotificationRegister(schema_version: "1", request_id: cache.requestID, data_epoch: status.data_epoch,
            scope_revision: revision, token: "", installation_id: installation?.id,
            expected_token_version: installation?.token_version ?? 0, expected_consent_revision: installation?.consent_revision, opt_in: optIn)
    }
    public func enable() async {
        guard active, !busy, device.capable, status?.enable_label != nil, cache?.pendingRevoke != true, let status else { return }
        busy = true; defer { busy = false }
        issue = nil; let captured = generation
        if cache == nil { cache = Cache(requestID: UUID().uuidString, epoch: status.data_epoch, binding: status.session_binding); save() }
        do {
            let allowed = try await device.requestPermission()
            guard active, captured == generation else { return }
            guard allowed else { markRevoke(); try await flushRevoke(); issue = "Notification permission is off. No report alerts are enabled."; return }
            registration = makeRegistration(optIn: true)
            if registration != nil { device.register() }
        } catch { if active, captured == generation { issue = "Notification permission or registration could not be confirmed." } }
    }
    public func didRegister(_ token: Data) async {
        guard active, var request = registration else { return }
        registration = nil; let captured = generation
        request.token = token.map { String(format: "%02x", $0) }.joined()
        do {
            let value = try await client.registerReportNotifications(request)
            guard active, captured == generation else { return }
            cache?.installationID = value.installation_id; save(); adopt(value.status); issue = nil
        } catch { if active, captured == generation { issue = "Device registration was not confirmed. Refresh and retry explicitly." } }
    }
    public func registrationFailed() { if active { registration = nil; issue = "APNs registration is unavailable. Check the signed build capability and network; no fallback is scheduled." } }
    private func markRevoke() {
        let current = installation
        generation += 1; registration = nil
        cache?.pendingRevoke = true
        cache?.revokeInstallationID = current?.id
        cache?.revokeConsentRevision = current?.consent_revision
        save(); device.unregister()
    }
    private func flushRevoke() async throws {
        guard let cache, cache.pendingRevoke else { return }
        let captured = generation
        let value: NotificationStatus
        do { value = try await client.revokeReportNotifications(NotificationRevoke(schema_version: "1", installation_id: cache.revokeInstallationID,
            registration_request_id: cache.requestID, expected_consent_revision: cache.revokeConsentRevision)) }
        catch {
            if active, captured == generation { issue = "Report alert revocation is not confirmed. It will retry after authentication is restored." }
            throw error
        }
        guard active, captured == generation else { return }
        self.cache?.pendingRevoke = false; adopt(value); issue = nil
        await device.clearDelivered()
    }
    public func disable() async {
        guard active, !busy else { return }
        busy = true; defer { busy = false }
        markRevoke()
        do { try await flushRevoke() }
        catch { issue = "Report alert revocation is not confirmed. It will retry on your next authenticated refresh." }
    }
    /// Called before SDK sign-out/account switching. Failure must block that transition
    /// rather than falsely claiming server revocation; local UI may be hidden immediately.
    public func clearDeviceAlerts() async { await device.clearDelivered() }
    public func stopForLogout() { markRevoke(); suspend() }
    public func confirmLogout() { cache = nil; save(); status = nil; issue = nil; active = false }
    public func logout() async throws {
        generation += 1; registration = nil; device.unregister()
        await device.clearDelivered()
        let value = try await client.logoutReportNotifications()
        guard value.revoked else { throw ClientFailure.invalidResponse }
        cache = nil; save(); status = nil; issue = nil; active = false
    }
}


/// Authenticated logout RPC usable after the main account cache is synchronously purged.
/// The caller supplies a separately fenced transport for the captured SDK session.
public enum ReportNotificationSessionLogout {
    @MainActor public static func perform(transport: any HTTPTransport, origin: URL) async throws {
        var request = URLRequest(url: origin.appendingPathComponent("report-notifications/v1/logout"))
        request.httpMethod = "POST"; request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try Wire.encode(NotificationLogout(schema_version: "1", request_id: UUID().uuidString))
        let response = try await transport.send(request)
        guard response.status == 200, try Wire.decode(NotificationLogoutResult.self, response.body).revoked else { throw ClientFailure.invalidResponse }
    }
}
