import SwiftUI
import CompanionCore
#if canImport(LinkKit)
import LinkKit
#endif

/// Account-scoped memory-only coordinator. Inventory policy and all action outcomes are server-owned.
@MainActor final class BankingController: ObservableObject {
    @Published private(set) var overview: BankingOverview?
    @Published private(set) var status: BankingViewStatus = .idle
    @Published private(set) var notice: String?
    @Published private(set) var session: PlaidLinkSession?
    @Published var showingLink = false
    var stateChanged: (() -> Void)?
    private let client: CompanionClient
    private var active = true
    private var admissionBlocked = false
    private var operation = UUID()
    private var callbackTicket = UUID()
    private var work: Task<Void, Never>?
    private var serverFlight: UUID?
    private var linkContext: BankingLinkContext?
    private var awaitingLink = false
    var working: Bool { status == .loading || status == .updating }
    var hasLinkSession: Bool { linkContext != nil }
    init(client: CompanionClient) { self.client = client }

    private func valid(_ id: UUID, epoch: String?) -> Bool {
        active && !admissionBlocked && id == operation && !Task.isCancelled
            && (epoch == nil || client.snapshot.state?.data_epoch == epoch)
    }
    private func validLink(_ ticket: UUID, context: BankingLinkContext) -> Bool {
        active && !admissionBlocked && awaitingLink && ticket == callbackTicket
            && client.snapshot.state?.data_epoch == context.epoch && client.snapshot.outbox.isEmpty
    }
    func cancelForLifecycle() {
        operation = UUID(); callbackTicket = UUID(); work?.cancel(); work = nil; serverFlight = nil
        awaitingLink = false; showingLink = false; session = nil; linkContext = nil
        overview = nil; notice = nil; status = .idle
        client.invalidateBankingWork()
    }
    func invalidateSession() { active = false; cancelForLifecycle() }
    func pauseForAdmission() {
        admissionBlocked = true; callbackTicket = UUID()
        let hadLink = awaitingLink
        awaitingLink = false; showingLink = false; session = nil; linkContext = nil
        overview = nil; client.clearBankingSnapshot()
        if serverFlight == nil {
            operation = UUID(); work?.cancel(); work = nil
            status = .failed("Secure access changed. Refresh bank accounts before continuing.")
            if hadLink { notice = "Bank setup closed on this device. Server cancellation is unconfirmed; no connection success is claimed." }
        } else { notice = "Checking secure access before confirming this bank action…" }
    }
    func admissionRestored() { admissionBlocked = false }

    func refresh(notify: Bool = true) async {
        guard active, !admissionBlocked, !working, linkContext == nil else { return }
        let id = UUID(); operation = id; status = .loading; notice = nil
        serverFlight = id
        defer { if serverFlight == id { serverFlight = nil } }
        do {
            try await client.refresh()
            let epoch = client.snapshot.state?.data_epoch
            var value: BankingOverview?
            for attempt in 0..<2 {
                do { value = try await client.refreshBanking(); break }
                catch BankingFailure.staleResponse {
                    guard attempt == 0 else { throw BankingFailure.staleResponse }
                    try await client.refresh()
                }
            }
            guard valid(id, epoch: epoch), let value else { throw BankingTransportFailure.interrupted }
            overview = value; status = .idle
            if notify { stateChanged?() }
        } catch {
            guard active, id == operation else { return }
            overview = nil; status = .failed(message(for: error))
        }
    }

    func perform(_ event: BankingEvent, requestID: String = UUID().uuidString) {
        guard active, !admissionBlocked, !working, linkContext == nil else { return }
        let id = UUID(), callback = callbackTicket
        operation = id; status = .updating; notice = nil
        let request = event.request(requestID: requestID) // Never regenerated inside retries.
        work = Task { [weak self] in
            guard let self else { return }
            serverFlight = id
            defer { if serverFlight == id { serverFlight = nil } }
            do {
                let response = try await client.banking(request)
                guard valid(id, epoch: event.version.data_epoch) else { throw BankingTransportFailure.interrupted }
                overview = client.bankingSnapshot
                guard let result = response.result else { throw ClientFailure.invalidResponse }
                notice = result.message.isEmpty ? "Bank action returned: \(result.status.rawValue.replacingOccurrences(of: "_", with: " "))." : result.message
                status = .idle; stateChanged?()
                switch event.action {
                case .addLink, .updateLink:
                    guard callback == callbackTicket else {
                        notice = "Bank setup was interrupted while secure access was checked. Refresh before starting a new setup; no connection success is confirmed."
                        return
                    }
                    try present(result, action: event.action, epoch: event.version.data_epoch, callback: callback)
                default:
                    // No implicit process/sync. Plaid-shared inventory is enabled by the server.
                    guard result.link_token == nil, result.expiration == nil else { throw ClientFailure.invalidResponse }
                }
            } catch {
                guard active, id == operation, !Task.isCancelled else { return }
                var explanation = message(for: error)
                // Reads only: refresh conflicts and ambiguous outcomes without retagging/replaying.
                do {
                    try await client.refresh()
                    let current = try await client.refreshBanking()
                    guard valid(id, epoch: nil) else { return }
                    overview = current; stateChanged?()
                } catch {
                    overview = nil
                    if error as? BankingTransportFailure == .profileUnavailable { explanation = message(for: error) }
                }
                guard active, id == operation else { return }
                if case .cancelLink = event.action {
                    explanation = "Setup closed on this device. Server cancellation is unconfirmed. " + explanation
                }
                status = .failed(explanation); notice = explanation
            }
        }
    }

    private func present(_ result: BankingResult, action: BankingAction, epoch: String, callback: UUID) throws {
        guard result.status == .link_ready, let sessionID = result.link_session_id,
              let token = result.link_token, token.hasPrefix("link-sandbox-"),
              let rawExpiry = result.expiration, let expiry = PlaidReply.date(rawExpiry), expiry > Date() else {
            throw BankingTransportFailure.invalidLinkSession
        }
        let mode: BankingLinkContext.Mode
        switch action {
        case .addLink: mode = .add
        case .updateLink(let connection): mode = .update(connectionID: connection)
        default: throw BankingTransportFailure.invalidLinkSession
        }
        let context = BankingLinkContext(mode: mode, sessionID: sessionID, epoch: epoch)
        linkContext = context; awaitingLink = true; status = .updating
        let config = LinkTokenConfiguration(token: token, onSuccess: { [weak self] success in
            // Update mode never consumes the callback public token or metadata.
            let publicToken: String?
            switch context.mode { case .add: publicToken = success.publicToken; case .update: publicToken = nil }
            Task { @MainActor [weak self] in self?.succeeded(context, callback: callback, publicToken: publicToken) }
        }, onExit: { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.validLink(callback, context: context) else { return }
                self.cancelLink()
            }
        }, onEvent: nil, onLoad: { [weak self] in
            Task { @MainActor [weak self] in
                guard let self, self.validLink(callback, context: context) else { return }
                guard expiry > Date() else { self.cancelLink(); return }
                self.showingLink = true
            }
        })
        do { session = try Plaid.createPlaidLinkSession(configuration: config) }
        catch {
            awaitingLink = false; showingLink = false; session = nil
            status = .failed("Native bank setup could not open. Cancel setup before trying again.")
        }
    }
    private func closePresentation() {
        callbackTicket = UUID(); awaitingLink = false; showingLink = false; session = nil; linkContext = nil; status = .idle
    }
    private func succeeded(_ context: BankingLinkContext, callback: UUID, publicToken: String?) {
        guard validLink(callback, context: context) else { return }
        closePresentation()
        do {
            guard let version = client.bankingSnapshot?.version else { throw BankingTransportFailure.staleAction }
            perform(try context.success(publicToken: publicToken, version: version))
        } catch { status = .failed(message(for: error)) }
    }
    func cancelLink() {
        guard active, !admissionBlocked, let context = linkContext else { return }
        closePresentation()
        guard let version = client.bankingSnapshot?.version, version.data_epoch == context.epoch else {
            status = .failed("Setup closed locally. Server cancellation is unconfirmed because your records changed."); return
        }
        perform(BankingEvent(action: .cancelLink(linkSessionID: context.sessionID), version: version))
    }
    func sheetDismissed() { if awaitingLink { cancelLink() } }

    private func message(for error: Error) -> String {
        if let error = error as? BankingAPIError {
            switch error.code {
            case .stale_banking, .obsolete_selection, .epoch_mismatch, .idempotency_conflict:
                return "Bank accounts changed. Refresh and review current Plaid sharing before continuing. The original action was not retagged."
            case .not_configured: return "Bank management is unavailable. Ask the service operator to enable the banking profile."
            case .not_found: return "This bank or account is no longer available. Refresh bank accounts."
            case .busy: return "Bank management is busy. Refresh before trying again."
            default: return "The bank action was not confirmed. Refresh and review the current server state."
            }
        }
        if let failure = error as? BankingTransportFailure {
            switch failure {
            case .profileUnavailable: return "Bank management is unavailable on this service. Enable the banking profile; the legacy bank endpoint will not be used."
            case .ambiguousExchange: return "Bank setup could not be confirmed. The one-use exchange was not replayed. Refresh and inspect existing banks before starting another setup."
            case .staleAction: return "Bank accounts changed. Refresh and choose again; the original action was not retagged."
            case .invalidLinkSession: return "The bank setup session is invalid or expired. Refresh before starting again."
            case .interrupted: return "Bank action interrupted. Its result is unconfirmed; refresh before continuing."
            }
        }
        if error is AuthenticationFailure { return "Secure access changed. Refresh bank accounts after signing in; the result is unconfirmed." }
        if error is URLError { return "Bank service could not be reached. No action is queued offline; refresh before continuing." }
        if error is BankingFailure { return "Bank accounts changed or the service response was incompatible. Refresh before continuing." }
        if error as? ClientFailure == .pendingChanges { return "Unsent changes are waiting. Open Settings → Your data to save or discard them before managing bank accounts." }
        if error is ClientFailure { return "Another change is pending or the bank response could not be confirmed. Refresh before continuing." }
        return "Bank action could not be confirmed. Refresh before continuing."
    }
}
