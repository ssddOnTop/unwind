import Foundation
import Testing
@testable import CompanionCore

@MainActor private final class NotificationDeviceFake: ReportNotificationDevice {
    var capable = true
    var permission = true
    var permissions = 0, registrations = 0, unregistrations = 0, cleared = 0
    func authorized() async -> Bool { permission }
    func requestPermission() async throws -> Bool { permissions += 1; return permission }
    func register() { registrations += 1 }
    func unregister() { unregistrations += 1 }
    func clearDelivered() async { cleared += 1 }
}

@MainActor private final class NotificationHTTPFake: HTTPTransport {
    var state: CompanionCore.State
    var status: NotificationStatus
    var requests: [URLRequest] = []
    var nonce: String?
    var failRevoke = false
    var holdRegister = false
    var entered = false
    var continuation: CheckedContinuation<Void, Never>?
    let installationID = "00000000-0000-4000-8000-000000000010"
    init() throws {
        state = try fixture()
        status = NotificationStatus(schema_version: "1", data_epoch: state.data_epoch, scope_revision: 1,
            session_binding: String(repeating: "a", count: 64), session_revoked: false, selected_installation_id: nil,
            eligible: true, delivery_status: "ready", title: "Backend notification title", message: "Generic alerts only.",
            enable_label: "Enable report-ready alerts", revoke_label: "Turn off report-ready alerts", installations: [])
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        switch request.url?.path {
        case "/state": return HTTPResult(status: 200, body: try Wire.encode(state))
        case "/report-notifications/v1": return HTTPResult(status: 200, body: try Wire.encode(status))
        case "/report-notifications/v1/status":
            let body = try Wire.decode(NotificationStatusRequest.self, request.httpBody!)
            status.selected_installation_id = body.registration_request_id == nonce ? status.installations.first?.id : nil
            return HTTPResult(status: 200, body: try Wire.encode(status))
        case "/report-notifications/v1/register":
            let body = try Wire.decode(NotificationRegister.self, request.httpBody!)
            nonce = body.request_id
            let row = NotificationInstallation(id: installationID, enabled: true, token_version: body.expected_token_version + 1, consent_revision: body.expected_consent_revision ?? 1)
            status.installations = [row]
            let result = NotificationRegistration(schema_version: "1", installation_id: installationID, token_version: row.token_version, consent_revision: row.consent_revision, status: status)
            if holdRegister { entered = true; await withCheckedContinuation { continuation = $0 } }
            return HTTPResult(status: 200, body: try Wire.encode(result))
        case "/report-notifications/v1/revoke":
            if failRevoke { throw URLError(.notConnectedToInternet) }
            let body = try Wire.decode(NotificationRevoke.self, request.httpBody!)
            nonce = body.registration_request_id
            status.installations = [NotificationInstallation(id: installationID, enabled: false, token_version: 1, consent_revision: (body.expected_consent_revision ?? 1) + 1)]
            status.selected_installation_id = installationID
            return HTTPResult(status: 200, body: try Wire.encode(status))
        case "/report-notifications/v1/logout": return HTTPResult(status: 200, body: try Wire.encode(NotificationLogoutResult(schema_version: "1", revoked: true)))
        default: throw ClientFailure.invalidEndpoint
        }
    }
}

@Test @MainActor func notificationOptInTokenRotationAndRevokeUseServerCASWithoutCachingToken() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let name = "notification-tests-" + UUID().uuidString, defaults = UserDefaults(suiteName: name)!
    defer { defaults.removePersistentDomain(forName: name) }
    let http = try NotificationHTTPFake(), device = NotificationDeviceFake(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh()
    let controller = ReportNotificationController(client: client, device: device, accountNamespace: name, defaults: defaults)
    await controller.refresh(); #expect(device.permissions == 0 && device.registrations == 0)
    await controller.enable(); #expect(device.permissions == 1 && device.registrations == 1)
    let token = Data(repeating: 0xab, count: 32)
    await controller.didRegister(token)
    #expect(controller.enabled)
    let initial = try Wire.decode(NotificationRegister.self, http.requests.last!.httpBody!)
    #expect(initial.opt_in && initial.installation_id == nil && initial.expected_consent_revision == nil)
    #expect(initial.expected_token_version == 0)
    for value in defaults.dictionaryRepresentation().values {
        if let data = value as? Data { #expect(!(String(data: data, encoding: .utf8) ?? "").contains(initial.token)) }
    }
    await controller.refresh(); #expect(device.permissions == 1 && device.registrations == 2)
    await controller.didRegister(Data(repeating: 0xcd, count: 32))
    let rotation = try Wire.decode(NotificationRegister.self, http.requests.last!.httpBody!)
    #expect(!rotation.opt_in)
    #expect(rotation.installation_id == http.installationID && rotation.expected_token_version == 1 && rotation.expected_consent_revision == 1)
    await controller.disable(); #expect(!controller.enabled)
    let count = http.requests.count
    await controller.didRegister(token); #expect(http.requests.count == count)
    await controller.refresh(); #expect(device.registrations == 2)
}

@Test @MainActor func notificationLateRegistrationCannotResurrectRevokeAndFailedRevokeRetries() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let name = "notify-race-" + UUID().uuidString, defaults = UserDefaults(suiteName: name)!
    defer { defaults.removePersistentDomain(forName: name) }
    let http = try NotificationHTTPFake(), device = NotificationDeviceFake(), client = try CompanionClient(storage: storage, transport: http)
    try await client.refresh()
    let controller = ReportNotificationController(client: client, device: device, accountNamespace: name, defaults: defaults)
    await controller.refresh(); await controller.enable(); http.holdRegister = true
    let task = Task { await controller.didRegister(Data(repeating: 0xaa, count: 32)) }
    for _ in 0..<100 where !http.entered { await Task.yield() }
    #expect(http.entered)
    http.failRevoke = true; await controller.disable()
    #expect(controller.issue?.contains("not confirmed") == true)
    http.failRevoke = false; await controller.refresh()
    #expect(!controller.enabled)
    http.continuation?.resume(); await task.value
    #expect(!controller.enabled && device.permissions == 1)
    controller.stopForLogout()
    let count = http.requests.count; await controller.didRegister(Data(repeating: 0xbb, count: 32))
    #expect(http.requests.count == count)
}

@Test @MainActor func notificationUnavailableBuildAndSessionSwitchNeverAutoOptIn() async throws {
    let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
    let http = try NotificationHTTPFake(), device = NotificationDeviceFake(), client = try CompanionClient(storage: storage, transport: http)
    device.capable = false; try await client.refresh()
    let controller = ReportNotificationController(client: client, device: device, accountNamespace: UUID().uuidString)
    await controller.refresh(); await controller.enable(); await controller.didRegister(Data([0xab]))
    #expect(device.permissions == 0 && device.registrations == 0)
    #expect(controller.issue?.contains("unavailable in this build") == true)
    #expect(!http.requests.contains { $0.url?.path == "/report-notifications/v1/register" })
    var object = try dictionary(http.status); object["token"] = "forbidden"
    #expect(throws: (any Error).self) { try Wire.decode(NotificationStatus.self, JSONSerialization.data(withJSONObject: object)) }
    try await ReportNotificationSessionLogout.perform(transport: http, origin: URL(string: "http://127.0.0.1:8769")!)
    let body = try #require(JSONSerialization.jsonObject(with: http.requests.last!.httpBody!) as? [String: Any])
    #expect(Set(body.keys) == ["schema_version", "request_id"])
    controller.suspend(); await controller.didRegister(Data([0xcd]))
    #expect(!http.requests.contains { $0.url?.path == "/report-notifications/v1/register" })
}
