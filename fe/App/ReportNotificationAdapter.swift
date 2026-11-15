import Foundation
import UserNotifications
import UIKit
import CompanionCore

/// APNs-only device boundary. No schedules, local fallback or permission work at init.
@MainActor final class ReportNotificationAdapter: ReportNotificationDevice {
    private let center = UNUserNotificationCenter.current()
    var capable: Bool {
        #if targetEnvironment(simulator)
        return false
        #else
        return (Bundle.main.object(forInfoDictionaryKey: "ReportNotificationsCapability") as? String) == "YES"
        #endif
    }
    func bind(_ controller: ReportNotificationController) { ReportNotificationCallbacks.current = controller }
    func authorized() async -> Bool {
        let value = await center.notificationSettings()
        return value.authorizationStatus == .authorized || value.authorizationStatus == .provisional
    }
    func requestPermission() async throws -> Bool {
        guard capable else { return false }
        return try await center.requestAuthorization(options: [.alert, .sound])
    }
    func register() { if capable { UIApplication.shared.registerForRemoteNotifications() } }
    func unregister() { if capable { UIApplication.shared.unregisterForRemoteNotifications() } }
    func clearDelivered() async {
        let delivered = await center.deliveredNotifications()
        center.removeDeliveredNotifications(withIdentifiers: delivered.filter {
            $0.request.content.userInfo["report_notification_id"] != nil || $0.request.identifier.hasPrefix("report-ready:")
        }.map { $0.request.identifier })
        // Clean only old report-ready local intents, never schedule a fallback.
        let pending = await center.pendingNotificationRequests()
        center.removePendingNotificationRequests(withIdentifiers: pending.map(\.identifier).filter { $0.hasPrefix("report-ready:") })
    }
}

@MainActor private enum ReportNotificationCallbacks {
    static weak var current: ReportNotificationController?
}

@MainActor final class ReportNotificationApplicationDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        guard let controller = ReportNotificationCallbacks.current else { return }
        Task { await controller.didRegister(deviceToken) }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        // Never display/log the raw platform error, token or signed request.
        ReportNotificationCallbacks.current?.registrationFailed()
    }
}
