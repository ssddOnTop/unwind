import SwiftUI

// All service state and effects belong to the integrating parent. These views never
// request permissions, connect services, seed data or persist consent on appearance.

struct ExperienceAction {
    let title: String
    var detail: String? = nil
    let perform: () -> Void
}

struct ExperienceIssue {
    /// Supply short, user-facing recovery copy, never a raw service error.
    let message: String
    let recovery: ExperienceAction
}

struct AccessibleInfoPopover: View {
    let title: String
    let message: String
    @State private var isPresented = false

    var body: some View {
        Button { isPresented = true } label: {
            Image(systemName: "info.circle")
                .font(.body)
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(.secondary)
        .accessibilityLabel("About \(title)")
        .accessibilityIdentifier("info.\(title)")
        .accessibilityHint("Shows more information")
        .popover(isPresented: $isPresented) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(title).font(.headline).accessibilityAddTraits(.isHeader)
                    Text(message).font(.body).fixedSize(horizontal: false, vertical: true)
                    Button("Done") { isPresented = false }
                        .accessibilityIdentifier("info.dismiss.\(title)")
                        .buttonStyle(.bordered)
                        .frame(minHeight: 44)
                }
                .padding(24)
            }
            .frame(idealWidth: 320, idealHeight: 280)
            .textCase(nil)
            .presentationCompactAdaptation(.popover)
        }
    }
}

private struct ExperienceBadge: View {
    let title: String
    var body: some View {
        Text(title)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(.quaternary, in: Capsule())
            .fixedSize(horizontal: false, vertical: true)
    }
}

private struct ExperiencePrimaryButton: View {
    let action: ExperienceAction
    var isBusy = false
    var body: some View {
        Button(action: action.perform) {
            Text(action.title)
                .font(.headline)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity, minHeight: 44)
                .padding(.horizontal, 8)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .disabled(isBusy)
    }
}

/// Content for the Today tab. The parent owns navigation; no ledger appears here.
/// Supply one supported observation and only an eligible action (or a connect action).
/// A visible error replaces the primary action with recovery, keeping one clear next step.
enum ExperienceConnectionKind {
    case bank, appleHealth, reminders

    var title: String {
        switch self {
        case .bank: "Bank"
        case .appleHealth: "Apple Health"
        case .reminders: "Reminders"
        }
    }

    var symbol: String {
        switch self {
        case .bank: "building.columns"
        case .appleHealth: "heart"
        case .reminders: "bell"
        }
    }
}

enum ExperienceConnectionState {
    case notConnected, working, connected, accessNotConfirmed, unavailable, needsAttention
}

struct ExperienceConnection {
    let kind: ExperienceConnectionKind
    var state: ExperienceConnectionState = .notConnected
    /// Explicit environment input: do not infer a live connection from a completed request.
    var isSandbox = false
    /// Critical failure and recovery instructions stay visible, not in a popover.
    var error: String? = nil

    var statusText: String {
        switch state {
        case .notConnected: kind == .reminders ? "Not set up" : "Not connected"
        case .working: "Working…"
        case .connected:
            switch kind {
            case .bank: "Connected"
            case .appleHealth: "Step records available"
            case .reminders: "Permission allowed"
            }
        case .accessNotConfirmed: "Access not confirmed"
        case .unavailable: "Not available right now"
        case .needsAttention: "Needs attention"
        }
    }
}

struct ConnectionActions {
    var connect: (() -> Void)? = nil
    var disconnect: (() -> Void)? = nil
}

/// Reusable in onboarding and Settings. Apple Health always presents material
/// sharing consent before invoking connect, including a retry after an error.
struct ConnectionRow: View {
    let connection: ExperienceConnection
    let actions: ConnectionActions
    @State private var showingHealthConsent = false

    private var canConnect: Bool {
        connection.state != .connected && connection.state != .working && connection.state != .unavailable
    }

    private var connectTitle: String {
        if connection.state == .needsAttention { return "Try again" }
        switch connection.kind {
        case .bank: return "Connect bank"
        case .appleHealth: return connection.state == .accessNotConfirmed ? "Review Health access" : "Connect Apple Health"
        case .reminders: return "Allow reminders"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: connection.kind.symbol)
                    .font(.title3).frame(width: 28, height: 28).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 5) {
                    Text(connection.kind.title).font(.headline)
                    Text(connection.statusText).font(.subheadline).foregroundStyle(.secondary)
                    if connection.isSandbox { ExperienceBadge(title: "Sandbox") }
                }
                Spacer(minLength: 0)
                if connection.state == .working { ProgressView().accessibilityLabel("Working on \(connection.kind.title)") }
            }
            if connection.kind == .appleHealth && connection.state == .accessNotConfirmed {
                Text("A completed permission request doesn’t confirm read access.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            if let error = connection.error {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.subheadline).fixedSize(horizontal: false, vertical: true)
            }
            // Vertical controls continue to fit at accessibility text sizes.
            if let connect = actions.connect, canConnect {
                Button(connectTitle) {
                    if connection.kind == .appleHealth { showingHealthConsent = true }
                    else { connect() }
                }
                .buttonStyle(.bordered).frame(minHeight: 44)
                .accessibilityLabel(connection.state == .needsAttention ? "Retry \(connection.kind.title)" : connectTitle)
            }
            if let disconnect = actions.disconnect {
                Button(connection.kind == .reminders ? "Turn off reminders" : "Disconnect", role: .destructive, action: disconnect)
                    .buttonStyle(.borderless).frame(minHeight: 44)
                    .accessibilityLabel("Disconnect \(connection.kind.title)")
            }
        }
        .padding(.vertical, 8)
        .sheet(isPresented: $showingHealthConsent) {
            HealthSharingConsent(
                isBusy: connection.state == .working || connection.state == .unavailable,
                onContinue: {
                    showingHealthConsent = false
                    actions.connect?()
                },
                onCancel: { showingHealthConsent = false }
            )
        }
    }
}

/// This is an application-sharing decision, before Apple's separate OS permission.
/// The parent must persist consent and request OS access only from onContinue.
struct HealthSharingConsent: View {
    var isBusy = false
    let onContinue: () -> Void
    let onCancel: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Text("Share your step totals?").font(.title.bold()).accessibilityAddTraits(.isHeader)
                Text("Share daily step totals for the past seven completed days with this app and its local service.")
                Text("Apple will ask separately which Health data you allow this app to read. Nothing is written to Apple Health. This does not enable cloud sharing.")
                    .foregroundStyle(.secondary)
                Text("You can disconnect in Settings. Disconnecting stops future use; it does not delete previously shared records.")
                    .font(.subheadline).foregroundStyle(.secondary)
                ExperiencePrimaryButton(
                    action: ExperienceAction(title: "Agree and continue to Apple Health", perform: onContinue),
                    isBusy: isBusy
                )
                Button("Not now", action: onCancel).frame(maxWidth: .infinity, minHeight: 44)
            }
            .frame(maxWidth: 560, alignment: .leading)
            .padding(24)
            .frame(maxWidth: .infinity)
        }
    }
}

/// Insert directly into a Settings List or Form. Callback availability is parent-owned.
struct ConnectionsSection: View {
    let bank: ExperienceConnection
    let health: ExperienceConnection
    let bankActions: ConnectionActions
    let healthActions: ConnectionActions

    var body: some View {
        Section {
            ConnectionRow(connection: bank, actions: bankActions)
            ConnectionRow(connection: health, actions: healthActions)
        } header: {
            HStack {
                Text("Connections")
                Spacer()
                AccessibleInfoPopover(
                    title: "Connections",
                    message: "Connect or disconnect here whenever you choose. Disconnecting stops future use; previously shared records remain until you delete them in data controls. Bank access does not enable cloud sharing."
                )
            }
        } footer: {
            Text("Optional. You can connect or disconnect later in Settings.")
        }
    }
}

/// Keep separate from bank and Health consent. Initial consent is off unless the
/// parent supplies a previously persisted opt-in; never derive it from connections.
struct CloudConsentSection: View {
    var isEnabled = false
    var isBusy = false
    let onChange: (Bool) -> Void

    var body: some View {
        Section {
            Toggle("Allow cloud suggestions", isOn: Binding(get: { isEnabled }, set: { onChange($0) }))
                .disabled(isBusy)
            Text("Optional wording from eligible Sandbox coverage summaries. Separate from your bank and Health connections.")
                .font(.subheadline).foregroundStyle(.secondary)
        } header: {
            HStack { Text("Cloud sharing"); Spacer(); AccessibleInfoPopover(title: "Cloud sharing", message: "Off by default. Enabling this allows eligible Sandbox coverage summaries to be sent to an external AI service for optional wording. Live Apple Health records are excluded. Bank consent never turns this on, and you can turn it off at any time.") }
        }
    }
}
