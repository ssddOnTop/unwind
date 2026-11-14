import SwiftUI
import CompanionCore
#if canImport(LinkKit)
import LinkKit
#endif

/// Session and credential-bearing tasks live only in memory, never in the persisted outbox.
@MainActor final class PlaidController: ObservableObject {
    @Published private(set) var session: PlaidLinkSession?
    @Published var showingLink = false
    @Published private(set) var working = false
    @Published private(set) var message = "Not connected by this session. Server source status is shown below."
    @Published private(set) var lastSync: PlaidSyncResponseResult?
    @Published private(set) var lastIssue: String?
    @Published private(set) var updateNotice: String?
    var stateChanged: (() -> Void)?
    private let client: CompanionClient
    private var work: Task<Void, Never>?
    private var ticket = UUID()
    private var linkTicket = UUID()
    private var admissionRevision = 0
    private var serverFlight: UUID?
    private var foreground = true
    private var linkAwaitingResult = false
    private(set) var revoking = false
    init(client: CompanionClient) { self.client = client }

    func cancel(_ message: String = "Connection work stopped. Refresh or connect explicitly to continue.") {
        let interrupted = working || linkAwaitingResult || serverFlight != nil
        ticket = UUID(); linkTicket = UUID(); serverFlight = nil
        work?.cancel(); work = nil; revoking = false
        client.invalidatePlaidWork()
        linkAwaitingResult = false; showingLink = false; session = nil; working = false
        lastSync = nil; lastIssue = nil; self.message = message
        updateNotice = interrupted ? message : nil
    }
    /// SDK callbacks and poll continuations are fenced independently from an in-flight HTTP
    /// operation. Only the transport may re-admit/retry that exact operation; client/session/
    /// epoch checks still precede its result. Never restore an old Link callback's generation.
    func pauseForAdmission() {
        let interrupted = working || linkAwaitingResult || serverFlight != nil
        admissionRevision += 1; linkTicket = UUID()
        linkAwaitingResult = false; showingLink = false; session = nil; lastSync = nil; lastIssue = nil
        if serverFlight == nil {
            ticket = UUID(); work?.cancel(); work = nil; working = false; revoking = false
            updateNotice = interrupted ? "Bank update was interrupted while secure access was checked. No new result is confirmed." : updateNotice
        } else {
            // Preserve only this HTTP operation, not an SDK callback or subsequent poll.
            updateNotice = "Checking secure access before confirming the bank update…"
        }
        message = updateNotice ?? "Secure access is being checked."
    }
    private func request(_ action: PlaidAction, epoch: String) async throws -> PlaidReply {
        let flight = UUID(); serverFlight = flight
        defer { if serverFlight == flight { serverFlight = nil } }
        return try await client.plaid(action, epoch: epoch)
    }
    private func validLink(_ id: UUID, callback: UUID, epoch: String) -> Bool {
        callback == linkTicket && valid(id, epoch: epoch)
    }
    func setForeground(_ active: Bool) {
        foreground = active
        if !active { cancel("Paused in background. Sync again manually when you return.") }
    }
    func sheetDismissed() {
        if linkAwaitingResult { cancel("Link cancelled. No connection success was claimed; disconnect can revoke app consent.") }
    }
    private func valid(_ id: UUID, epoch: String) -> Bool {
        id == ticket && foreground && !Task.isCancelled && client.snapshot.state?.data_epoch == epoch && client.snapshot.outbox.isEmpty
    }
    private func display(_ error: Error) {
        // Never render raw SDK/provider errors, token values, account metadata or HTTP bodies.
        if case PlaidFailure.ambiguousExchange = error {
            message = "We couldn’t confirm bank setup. Disconnect, then reconnect. Remote Sandbox cleanup may be unconfirmed."
        } else if case ClientFailure.rejected(let code, _) = error {
            switch code {
            case "not_configured": message = "Bank connections aren’t available right now. Try again after the local service is configured."
            case "not_found": message = "No bank is connected. Choose Connect bank to get started."
            case "busy": message = "Connection work is busy. Wait, then try explicitly; do not open another Link session."
            case "consent_required": message = "Sandbox consent or connection is missing. Connect again explicitly."
            default: message = "Sandbox action was rejected. Refresh source status before trying again."
            }
        } else if case PlaidFailure.expiredLinkToken = error {
            message = "Bank setup expired. Please connect again."
        } else if error is AuthenticationFailure {
            message = "Bank update was interrupted while secure access was checked. Its result is unconfirmed; review current records before trying again."
        } else if error is CancellationError || error is PlaidFailure {
            message = "Your connection changed while updating. Refresh, then reconnect if needed."
        } else {
            message = "Your bank connection couldn’t be updated. Try again, or disconnect and reconnect."
        }
        lastIssue = message; updateNotice = message
    }
    private func handleFailure(_ error: Error, id: UUID) async {
        guard id == ticket, !Task.isCancelled else { return }
        // Error envelopes do not carry state; fetch current source status without replaying the action.
        try? await client.refresh()
        stateChanged?()
        if id == ticket { display(error) }
    }
    func connect() {
        cancel()
        guard foreground, let epoch = client.snapshot.state?.data_epoch else { return }
        let id = ticket, callback = linkTicket; working = true; message = "Requesting a Sandbox Link session…"
        work = Task { [weak self] in
            guard let self else { return }
            do {
                let reply = try await request(.linkToken, epoch: epoch)
                stateChanged?()
                guard validLink(id, callback: callback, epoch: epoch), case .linkToken(let token) = reply.result else {
                    if id == ticket {
                        working = false
                        updateNotice = "Bank setup was interrupted while secure access was checked. No connection success is confirmed."
                        message = updateNotice!
                    }
                    return
                }
                let config = LinkTokenConfiguration(token: token.link_token, onSuccess: { [weak self] success in
                    // Copy only the Link-origin public token. Never inspect/log metadata.
                    let publicToken = success.publicToken
                    Task { @MainActor [weak self] in self?.exchange(publicToken, id: id, callback: callback, epoch: epoch) }
                }, onExit: { [weak self] exit in
                    let failed = exit.error != nil
                    Task { @MainActor [weak self] in
                        guard let self, self.linkAwaitingResult, self.validLink(id, callback: callback, epoch: epoch) else { return }
                        self.cancel(failed ? "Bank setup couldn’t finish. Please try connecting again." : "Bank setup cancelled.")
                        if failed { self.lastIssue = self.message }
                    }
                }, onEvent: nil, onLoad: { [weak self] in
                    Task { @MainActor [weak self] in
                        guard let self, self.linkAwaitingResult, self.validLink(id, callback: callback, epoch: epoch) else { return }
                        guard let expiration = PlaidReply.date(token.expiration), expiration > Date() else {
                            self.cancel("Link token expired. Connect again explicitly."); return
                        }
                        self.showingLink = true
                        self.message = "Native Sandbox Link ready. Use only synthetic test credentials."
                    }
                })
                linkAwaitingResult = true
                session = try Plaid.createPlaidLinkSession(configuration: config)
                // Retained before presentation; onLoad controls presentation. No synthetic success path.
            } catch {
                await handleFailure(error, id: id)
                if id == ticket { working = false; session = nil }
            }
        }
    }
    private func exchange(_ token: String, id: UUID, callback: UUID, epoch: String) {
        guard validLink(id, callback: callback, epoch: epoch), linkAwaitingResult else { return }
        linkAwaitingResult = false; showingLink = false; session = nil
        work = Task { [weak self] in
            guard let self else { return }
            do {
                let admission = admissionRevision
                _ = try await request(.exchange(publicToken: token), epoch: epoch)
                stateChanged?()
                guard valid(id, epoch: epoch) else { return }
                message = "Sandbox connected. Import readiness is unknown until sync completes."
                if admission != admissionRevision {
                    updateNotice = "Bank setup was confirmed after secure access was restored. Update bank records to check import readiness."
                    working = false; return
                }
                try await syncLoop(id: id, epoch: epoch)
            } catch { await handleFailure(error, id: id) }
            if id == ticket { working = false }
        }
    }
    func sync() {
        cancel()
        guard foreground, let epoch = client.snapshot.state?.data_epoch else {
            updateNotice = "Bank update could not start. Open the app and refresh your records first."; return
        }
        let id = ticket; working = true
        work = Task { [weak self] in
            guard let self else { return }
            do { try await syncLoop(id: id, epoch: epoch) }
            catch { await handleFailure(error, id: id) }
            if id == ticket { working = false }
        }
    }
    private func syncLoop(id: UUID, epoch: String) async throws {
        var plan = PlaidPollPlan()
        while valid(id, epoch: epoch) {
            guard client.snapshot.state?.sources.contains(where: { $0.id == "plaid" && $0.enabled && $0.consent == "granted" }) == true else { throw PlaidFailure.staleWork }
            message = "Syncing synthetic bank records…"
            // Each iteration creates a NEW logical request ID; transport retries preserve its bytes.
            let admission = admissionRevision
            let reply = try await request(.sync, epoch: epoch)
            stateChanged?()
            guard valid(id, epoch: epoch), case .sync(let result) = reply.result else { return }
            lastSync = result; lastIssue = nil; updateNotice = nil
            message = "Import: \(result.status). Empty/waiting is not evidence of zero purchases."
            if result.status == "error" {
                lastIssue = "The server could not finish the bank update. Import readiness is unconfirmed."
                updateNotice = lastIssue; return
            }
            if admission != admissionRevision {
                updateNotice = result.status == "complete" ? "Bank update confirmed after secure access was restored." : "Secure access was restored, but bank records are still pending. Automatic updates paused."
                return
            }
            guard let delay = plan.nextDelay(result: result, foreground: foreground, connected: true) else {
                if result.status != "complete" { message += " Automatic attempts ended. Use Sync again." }
                return
            }
            message += " Next foreground attempt in \(delay)s (\(plan.attempts)/5)."
            try await Task.sleep(for: .seconds(delay))
        }
    }
    func disconnect() {
        cancel()
        guard let epoch = client.snapshot.state?.data_epoch else { return }
        let id = ticket; working = true; revoking = true; message = "Revoking Sandbox connection…"
        work = Task { [weak self] in
            guard let self else { return }
            do {
                let reply = try await request(.disconnect, epoch: epoch)
                stateChanged?()
                guard id == ticket, case .disconnected(let status) = reply.result else { return }
                message = status == "disconnected" ? "Sandbox disconnected. Historical Sandbox evidence remains until retention/deletion." : "Consent revoked; remote removal pending. Retry Disconnect explicitly. No further collection is allowed."
            } catch { await handleFailure(error, id: id) }
            if id == ticket { working = false; revoking = false }
        }
    }
}

#if os(iOS)
struct PlaidSettingsSection: View {
    @ObservedObject var model: AppModel
    @ObservedObject var controller: PlaidController
    @SwiftUI.State private var consent = false
    @SwiftUI.State private var revoke = false
    var body: some View {
        Section("Plaid Sandbox · synthetic bank data") {
            Text("Native Link connects synthetic Sandbox institutions only. No real bank or Production login. Use a non-OAuth test institution with user_good / pass_good.")
            Text("Count-only merchant classifications, not spending, meals or food consumption. Household attribution starts unknown. Bank consent never enables cloud AI.").font(.caption)
            Text(controller.message).accessibilityIdentifier("plaid-status")
            LabeledContent("Stored Sandbox evidence revisions", value: String(model.state?.observations.filter { $0.source_id == "plaid" && $0.mode == "fixture" }.count ?? 0))
            Text("Revision count includes history and removals, not spending or consumption. Zero stored records can mean missing or not-yet-ready data.").font(.caption)
            if let counts = controller.lastSync {
                Text("Last committed cycle: added \(counts.added), modified \(counts.modified), removed \(counts.removed), skipped \(counts.skipped).")
                Text("Import readiness: \(counts.status) · coverage remains uncertain.").font(.caption)
            }
            Button("Connect Plaid Sandbox (synthetic bank data)") { consent = true }
                .disabled(controller.working || model.state == nil || model.hasPending)
            Button("Sync again") { controller.sync() }
                .disabled(controller.working || model.hasPending || !(model.state?.sources.contains { $0.id == "plaid" && $0.enabled && $0.consent == "granted" } ?? false))
            Button("Stop waiting") { controller.cancel("Stopped foreground polling. Sync again manually; no background work is promised.") }.disabled(!controller.working)
            Button("Disconnect Plaid Sandbox", role: .destructive) { revoke = true }
                .disabled(model.state == nil || model.hasPending)
        }
        .confirmationDialog("Consent to importing synthetic Sandbox bank classifications into the local backend? This is separate from cloud AI consent.", isPresented: $consent, titleVisibility: .visible) {
            Button("Consent and open native Sandbox Link") { controller.connect() }
            Button("Cancel", role: .cancel) {}
        }
        .confirmationDialog("Revoke Sandbox consent and remove the connection? Historical Sandbox records remain until deletion or retention.", isPresented: $revoke, titleVisibility: .visible) {
            Button("Disconnect Sandbox", role: .destructive) { controller.disconnect() }
            Button("Cancel", role: .cancel) {}
        }
        .sheet(isPresented: $controller.showingLink, onDismiss: { controller.sheetDismissed() }) {
            if let session = controller.session { session.sheet() }
        }
    }
}

#endif
