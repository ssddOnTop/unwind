import Foundation
import UserNotifications
import CompanionCore

@MainActor final class LocalReminders {
    private let center = UNUserNotificationCenter.current()
    private let accountPrefix: String
    private let prefix: String
    private var generation = 0
    private(set) var permission = "Not checked"
    init(accountNamespace: String) {
        accountPrefix = "account-" + accountNamespace + ":"
        prefix = accountPrefix + UUID().uuidString + ":"
    }
    func suspendCallbacks() { generation += 1 }
    func requestPermission() async throws {
        let captured = generation
        _ = try await center.requestAuthorization(options: [.alert, .sound])
        guard captured == generation else { return }
        await readPermission()
    }
    func readPermission() async {
        let captured = generation
        let settings = await center.notificationSettings()
        guard captured == generation else { return }
        switch settings.authorizationStatus {
        case .authorized: permission = "Authorized"
        case .provisional: permission = "Provisional"
        case .denied: permission = "Denied in iOS Settings"
        case .notDetermined: permission = "Not requested"
        case .ephemeral: permission = "Temporary"
        @unknown default: permission = "Unknown"
        }
    }
    func cancelAll() {
        generation += 1
        center.removeAllPendingNotificationRequests(); center.removeAllDeliveredNotifications()
    }
    func reconcile(state: CompanionCore.State?, locallyBlocked: Bool) async throws {
        let captured = generation
        guard let state else { cancelAll(); return }
        let settings = await center.notificationSettings(); await readPermission()
        guard captured == generation, !Task.isCancelled else { return }
        let authorized = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
        let pending = await center.pendingNotificationRequests()
        guard captured == generation, !Task.isCancelled else { return }
        // A late callback from a previous admission cannot delete this instance's new intent.
        center.removePendingNotificationRequests(withIdentifiers: pending.map(\.identifier).filter { $0.hasPrefix(accountPrefix) && !$0.hasPrefix(prefix) })
        let own = pending.map(\.identifier).filter { $0.hasPrefix(prefix) }.map { String($0.dropFirst(prefix.count)) }
        let plan = NotificationPlan.make(state: state, existing: own, now: Date(), authorized: authorized, locallyBlocked: locallyBlocked)
        center.removePendingNotificationRequests(withIdentifiers: plan.cancel.map { prefix + $0 })
        let desired = Set(plan.upsert.map { prefix + NotificationPlan.identifier(epoch: state.data_epoch, intent: $0.id) })
        let delivered = await center.deliveredNotifications()
        guard captured == generation, !Task.isCancelled else { return }
        center.removeDeliveredNotifications(withIdentifiers: delivered.map { $0.request.identifier }.filter { $0.hasPrefix(accountPrefix) && !desired.contains($0) })
        let formatter = ISO8601DateFormatter()
        for intent in plan.upsert {
            guard captured == generation, !Task.isCancelled else { return }
            guard let date = formatter.date(from: intent.scheduled_at) else { continue }
            let content = UNMutableNotificationContent()
            content.title = "Your companion"; content.body = "An optional suggestion is ready to review."; content.sound = .default
            var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(secondsFromGMT: 0)!
            var components = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date); components.timeZone = calendar.timeZone
            let id = prefix + NotificationPlan.identifier(epoch: state.data_epoch, intent: intent.id)
            let request = UNNotificationRequest(identifier: id, content: content, trigger: UNCalendarNotificationTrigger(dateMatching: components, repeats: false))
            try await center.add(request)
            if captured != generation || Task.isCancelled {
                // Remove only this old account's late scheduling result, never the new account's intent.
                center.removePendingNotificationRequests(withIdentifiers: [id]); return
            }
        }
    }
}
