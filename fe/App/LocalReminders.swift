import Foundation
import UserNotifications
import CompanionCore

@MainActor final class LocalReminders {
    private let center = UNUserNotificationCenter.current()
    private(set) var permission = "Not checked"
    func requestPermission() async throws {
        _ = try await center.requestAuthorization(options: [.alert, .sound])
        await readPermission()
    }
    func readPermission() async {
        let settings = await center.notificationSettings()
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
        center.removeAllPendingNotificationRequests()
        center.removeAllDeliveredNotifications()
    }
    func reconcile(state: CompanionCore.State?, locallyBlocked: Bool) async throws {
        guard let state else { cancelAll(); return }
        let settings = await center.notificationSettings()
        await readPermission()
        let authorized = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
        let pending = await center.pendingNotificationRequests()
        let plan = NotificationPlan.make(state: state, existing: pending.map(\.identifier), now: Date(), authorized: authorized, locallyBlocked: locallyBlocked)
        center.removePendingNotificationRequests(withIdentifiers: plan.cancel)
        // Remove delivered cards that no longer belong to the authoritative desired set as well.
        let desired = Set(plan.upsert.map { NotificationPlan.identifier(epoch: state.data_epoch, intent: $0.id) })
        let delivered = await center.deliveredNotifications()
        center.removeDeliveredNotifications(withIdentifiers: delivered.map { $0.request.identifier }.filter { !desired.contains($0) })
        let formatter = ISO8601DateFormatter()
        for intent in plan.upsert {
            guard let date = formatter.date(from: intent.scheduled_at) else { continue }
            let content = UNMutableNotificationContent()
            content.title = "Your companion"
            content.body = "An optional suggestion is ready to review."
            content.sound = .default
            var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(secondsFromGMT: 0)!
            var components = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
            components.timeZone = calendar.timeZone
            let trigger = UNCalendarNotificationTrigger(dateMatching: components, repeats: false)
            let request = UNNotificationRequest(identifier: NotificationPlan.identifier(epoch: state.data_epoch, intent: intent.id), content: content, trigger: trigger)
            try await center.add(request)
        }
    }
}
