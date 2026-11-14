import SwiftUI
import CompanionCore

/// Transport state is supplied by the owner; a failed fetch is not an empty inventory.
enum BankingViewStatus: Equatable {
    case idle
    case loading
    case updating
    /// Supply sanitized user-facing copy, never a raw transport/provider error.
    case failed(String)
}

/// Renderer only. The parent owns fetching, authentication, native Link, request IDs,
/// session fencing, and authoritative response publication. No preview or local data.
struct BankAccountsView: View {
    let overview: BankingOverview?
    let status: BankingViewStatus
    let onRefresh: () -> Void
    let onAction: (BankingEvent) -> Void

    @SwiftUI.State private var confirmation: Confirmation?

    private struct Confirmation {
        let title: String
        let message: String
        let button: String
        let destructive: Bool
        let event: BankingEvent
    }
    private var controlsDisabled: Bool { status != .idle || overview == nil }

    var body: some View {
        List {
            Section {
                HStack {
                    Text("Bank accounts").font(.headline)
                    Spacer()
                    if overview?.environment == .sandbox {
                        Text("Sandbox")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(.secondary.opacity(0.12), in: Capsule())
                            .accessibilityLabel("Sandbox — synthetic bank data")
                    }
                }
                transportStatus
                if let overview {
                    Text(verbatim: overview.selection_notice)
                        .font(.footnote).foregroundStyle(.secondary)
                    Text(verbatim: statusLabel(overview.status.rawValue))
                        .font(.caption).foregroundStyle(.secondary)
                    if let result = overview.result, !result.message.isEmpty {
                        Text(verbatim: result.message).font(.footnote)
                    }
                    Button("Add bank", systemImage: "plus") {
                        confirmation = Confirmation(
                            title: "Add a Sandbox bank?",
                            message: "Sandbox uses synthetic records, not a real-bank connection.",
                            button: "Continue in Sandbox", destructive: false,
                            event: BankingEvent(action: .addLink, version: overview.version)
                        )
                    }
                    .disabled(controlsDisabled || overview.status == .not_configured)
                    .accessibilityIdentifier("banking.add")
                    if !overview.connections.isEmpty {
                        Button("Update all banks") { emit(.sync(connectionID: nil), overview) }
                            .disabled(controlsDisabled || !overview.connections.contains(where: { $0.can_manage }))
                            .accessibilityIdentifier("banking.updateAll")
                    }
                }
            }

            if let overview {
                ForEach(overview.connections) { connection in
                    Section {
                        DisclosureGroup {
                            connectionControls(connection, overview: overview)
                            ForEach(connection.groups) { group in
                                groupControl(group, connection: connection, overview: overview)
                            }
                            ForEach(connection.accounts) { account in
                                accountRow(account, connection: connection, overview: overview)
                            }
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(verbatim: connection.displayName).font(.headline)
                                if let institution = connection.institution_name,
                                   !institution.isEmpty, institution != connection.displayName {
                                    Text(verbatim: institution).font(.subheadline)
                                }
                                Text(verbatim: statusLabel(connection.status.rawValue))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle("Bank accounts")
        .accessibilityIdentifier("banking.screen")
        .alert(confirmation?.title ?? "", isPresented: Binding(
            get: { confirmation != nil },
            set: { if !$0 { confirmation = nil } }
        ), presenting: confirmation) { pending in
            Button(pending.button, role: pending.destructive ? .destructive : nil) {
                // Do not silently retag a confirmation after inventory changes.
                guard !controlsDisabled, overview?.version == pending.event.version else { return }
                onAction(pending.event)
                confirmation = nil
            }
            Button("Cancel", role: .cancel) { confirmation = nil }
        } message: { pending in
            Text(verbatim: pending.message)
        }
        .onChange(of: overview?.version) { _, _ in confirmation = nil }
        .onChange(of: status) { _, new in if new != .idle { confirmation = nil } }
    }

    @ViewBuilder private var transportStatus: some View {
        switch status {
        case .loading:
            ProgressView("Loading bank accounts…")
        case .updating:
            ProgressView("Updating bank accounts…")
        case .failed(let message):
            Text(verbatim: message).foregroundStyle(.red)
                .accessibilityIdentifier("banking.error")
            Button("Try again", action: onRefresh)
        case .idle:
            if overview == nil { Text("Bank accounts have not been loaded.").foregroundStyle(.secondary) }
            Button("Refresh", action: onRefresh)
        }
    }

    private func connectionControls(_ connection: BankingConnection, overview: BankingOverview) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 16) { manageButtons(connection, overview: overview) }
                VStack(alignment: .leading, spacing: 12) { manageButtons(connection, overview: overview) }
            }
            Button("Disconnect bank", role: .destructive) {
                confirmation = Confirmation(
                    title: "Disconnect bank?",
                    message: connection.displayName + "\n\n" + overview.selection_notice
                        + "\n\nThis disconnects the whole bank connection, not just one account. It does not close your bank accounts.",
                    button: "Disconnect bank", destructive: true,
                    event: BankingEvent(action: .disconnect(connectionID: connection.id), version: overview.version)
                )
            }
            .disabled(controlsDisabled || !connection.can_disconnect)
            .accessibilityLabel(Text(verbatim: "Disconnect bank: " + connection.displayName))
        }
        .buttonStyle(.borderless)
    }

    @ViewBuilder private func manageButtons(_ connection: BankingConnection, overview: BankingOverview) -> some View {
        Button("Manage accounts") { emit(.updateLink(connectionID: connection.id), overview) }
            .disabled(controlsDisabled || !connection.can_manage)
            .accessibilityLabel(Text(verbatim: "Manage accounts: " + connection.displayName))
        Button("Update") { emit(.sync(connectionID: connection.id), overview) }
            .disabled(controlsDisabled || !connection.can_manage)
            .accessibilityLabel(Text(verbatim: "Update imports: " + connection.displayName))
    }

    private func groupControl(_ group: BankingGroup, connection: BankingConnection, overview: BankingOverview) -> some View {
        HStack {
            Text(verbatim: group.label).font(.subheadline)
            Spacer()
            Menu("Select") {
                Button("Enable group") {
                    emit(.selectGroup(connectionID: connection.id, groupID: group.id,
                                      accountIDs: group.account_ids, enabled: true), overview)
                }
                Button("Remove group", role: .destructive) {
                    emit(.selectGroup(connectionID: connection.id, groupID: group.id,
                                      accountIDs: group.account_ids, enabled: false), overview)
                }
            }
            .disabled(controlsDisabled || !connection.can_manage || group.account_ids.isEmpty)
            .accessibilityLabel(Text(verbatim: "Select accounts in " + group.label))
        }
    }

    private func accountRow(_ account: BankingAccount, connection: BankingConnection, overview: BankingOverview) -> some View {
        Toggle(isOn: Binding(
            get: { account.enabled },
            set: { enabled in
                emit(.selectAccounts(connectionID: connection.id, accountIDs: [account.id], enabled: enabled), overview)
            }
        )) {
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: account.displayName)
                if let official = account.official_name, !official.isEmpty, official != account.displayName {
                    Text(verbatim: official).font(.caption).foregroundStyle(.secondary)
                }
                Text(verbatim: [account.type, account.subtype].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary)
                if let mask = account.mask, !mask.isEmpty {
                    Text(verbatim: "•• " + mask).font(.caption.monospacedDigit())
                        .accessibilityLabel(Text(verbatim: "Account ending in " + mask))
                }
                Text(verbatim: statusLabel(account.availability.rawValue) + " · " + statusLabel(account.import_status.rawValue))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .disabled(controlsDisabled || !connection.can_manage || (!account.enabled && !account.can_enable))
        .accessibilityHint(account.enabled ? "Turn off to remove from future imports and new insights. Saved history stays." : "Turn on to enable imports.")
    }

    private func emit(_ action: BankingAction, _ overview: BankingOverview) {
        guard !controlsDisabled else { return }
        onAction(BankingEvent(action: action, version: overview.version))
    }
    private func statusLabel(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ").capitalized
    }
}
