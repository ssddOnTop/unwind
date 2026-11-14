import SwiftUI
import ClerkKit
import ClerkKitUI
import CompanionCore

@main struct LocalCompanionApp: App {
    @StateObject private var authentication = ClerkSessionCoordinator()
    var body: some Scene {
        WindowGroup {
            if let clerk = authentication.clerk {
                AuthenticationRoot(authentication: authentication).environment(clerk)
            } else {
                ContentUnavailableView("Sign-in setup needed", systemImage: "lock.shield", description: Text(authentication.issue ?? "Generate the approved public Clerk configuration and rebuild."))
                    .accessibilityIdentifier("auth.configurationMissing")
            }
        }
    }
}

private struct AuthenticationRoot: View {
    @Environment(Clerk.self) private var clerk
    @ObservedObject var authentication: ClerkSessionCoordinator
    @Environment(\.scenePhase) private var scenePhase
    @SwiftUI.State private var manageAccount = false
    private var identitySignal: String { "\(clerk.isLoaded)-\(clerk.session?.id ?? "none")-\(String(describing: clerk.session?.status))-\(clerk.user?.id ?? "none")" }
    var body: some View {
        Group {
            if !clerk.isLoaded {
                VStack(spacing: 20) {
                    ProgressView("Loading secure sign-in…")
                    if let issue = authentication.issue { Text(issue).font(.callout) }
                    Button("Try again") { Task { await authentication.retrySDK() } }
                }.padding().accessibilityIdentifier("auth.loading")
            } else if clerk.session?.status != .active || clerk.user == nil {
                // Native SDK owns all forms, challenges, session tasks and Keychain tokens.
                AuthView(mode: .signIn, isDismissible: false, onAuthComplete: authentication.authCompleted)
                    .accessibilityIdentifier("auth.nativeSignIn")
            } else if authentication.canShowAccountData, let model = authentication.model {
                AppRoot(model: model,
                    manageAccount: { authentication.prepareAccountManagement(); manageAccount = true },
                    signOut: authentication.signOut)
            } else {
                VStack(spacing: 20) {
                    Image(systemName: "lock.shield").font(.largeTitle)
                    Text(authentication.locallyLocked ? "Your records are locked" : "Opening your secure overview").font(.title2)
                    if let issue = authentication.issue { Text(issue).multilineTextAlignment(.center) }
                    else if !authentication.locallyLocked { ProgressView() }
                    if authentication.locallyLocked {
                        Button("Use this signed-in account", action: authentication.activateDeliberately)
                    } else { Button("Try again", action: authentication.retryService) }
                    Button("Manage sign-in") { authentication.prepareAccountManagement(); manageAccount = true }
                    Button("Sign out", role: .destructive, action: authentication.signOut)
                }.padding().accessibilityIdentifier("auth.locked")
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background { authentication.suspendForBackground() }
            else if phase == .active { authentication.resumeFromBackground() }
        }
        .task(id: identitySignal) { authentication.observeSDK() }
        .sheet(isPresented: $manageAccount) {
            NavigationStack {
                VStack(spacing: 16) {
                    Text("Local records have been cleared and remain locked while you manage your account.").font(.callout).padding()
                    // Expose SDK account switching only AFTER the host's synchronous local purge.
                    UserButton()
                    UserProfileView()
                }
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { manageAccount = false } } }
            }
        }
    }
}

enum CompanionTab: Hashable { case today, ledger, settings }

struct AppRoot: View {
    @ObservedObject var model: AppModel
    let manageAccount: () -> Void
    let signOut: () -> Void
    @Environment(\.scenePhase) private var scenePhase
    @SwiftUI.State private var tab: CompanionTab = .today
    @SwiftUI.State private var ledgerCard: String?
    @SwiftUI.State private var bankAccountsPresented = false
    @SwiftUI.State private var disconnectHealth = false
    private var healthActions: ConnectionActions {
        ConnectionActions(connect: { Task { await model.healthSync(connect: true) } }, disconnect: model.healthHasConsent ? { disconnectHealth = true } : nil)
    }
    var body: some View {
        TabView(selection: $tab) {
            BackendHomeView(model: model, openConnections: { tab = .settings; bankAccountsPresented = true }, openLedger: { card in ledgerCard = card; tab = .ledger })
                .tabItem { Label("Today", systemImage: "sun.max") }.tag(CompanionTab.today)
            LedgerView(model: model, selectedCard: $ledgerCard)
                .tabItem { Label("Ledger", systemImage: "list.bullet.rectangle") }.tag(CompanionTab.ledger)
            SettingsView(model: model, bankAccountsPresented: $bankAccountsPresented, healthActions: healthActions, manageAccount: manageAccount, signOut: signOut)
                .tabItem { Label("Settings", systemImage: "gearshape") }.tag(CompanionTab.settings)
        }
        .overlay { NativeBankingPresentation(controller: model.banking) }
        .confirmationDialog("Disconnect Apple Health?", isPresented: $disconnectHealth, titleVisibility: .visible) {
            Button("Disconnect Apple Health", role: .destructive) { Task { await model.disconnectHealth() } }
            Button("Cancel", role: .cancel) {}
        } message: { Text("Stops step sharing here. Previous records remain until deleted. Apple Health records and Apple's permission settings are unchanged.") }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await model.refresh() } }
            if phase == .background { model.banking.cancelForLifecycle() }
        }
    }
}

private struct NativeBankingPresentation: View {
    @ObservedObject var controller: BankingController
    var body: some View {
        Color.clear.frame(width: 0, height: 0)
            .sheet(isPresented: $controller.showingLink, onDismiss: { controller.sheetDismissed() }) {
                if let session = controller.session { session.sheet() }
            }
    }
}

struct CardSummary: View {
    let card: LedgerCard
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(verbatim: card.title).font(.headline)
            Text(verbatim: card.observed).foregroundStyle(.secondary)
            if card.mode == "fixture" { Text("Sample data").font(.caption) }
        }.padding(.vertical, 4)
    }
}
struct LedgerView: View {
    @ObservedObject var model: AppModel
    @Binding var selectedCard: String?
    var body: some View {
        NavigationStack {
            List {
                if let state = model.state {
                    ForEach(state.ledger, id: \.id) { card in
                        NavigationLink { LedgerDetail(model: model, cardID: card.id) } label: { CardSummary(card: card) }
                    }
                    if state.ledger.isEmpty { Text("Your records will appear here when available.") }
                } else { Text("Your records are not available yet. Try refreshing.") }
            }.navigationTitle("Ledger").accessibilityIdentifier("app.ledger")
                .navigationDestination(item: $selectedCard) { id in LedgerDetail(model: model, cardID: id) }
                .refreshable { await model.refresh() }
                .toolbar { AccessibleInfoPopover(title: "Ledger", message: "Evidence behind your observations lives here. Records may be incomplete; purchases do not prove what you ate, and missing activity is not zero activity.") }
        }
    }
}
