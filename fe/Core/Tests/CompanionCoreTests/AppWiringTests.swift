import Foundation
import Testing
@testable import CompanionCore

private var frontend: URL {
    URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
}

/// Structural regression guard for the actual SwiftUI root; not a signed-in runtime test.
@Test func actualAuthRootWiresBothScenePhaseAdmissionTransitions() throws {
    let source = try String(contentsOf: frontend.appendingPathComponent("App/LocalCompanionApp.swift"), encoding: .utf8)
    let start = try #require(source.range(of: "private struct AuthenticationRoot: View"))
    let end = try #require(source.range(of: "enum CompanionTab"))
    let root = String(source[start.lowerBound..<end.lowerBound])
    #expect(root.contains("@Environment(\\.scenePhase)"))
    #expect(root.contains(".onChange(of: scenePhase)"))
    #expect(root.contains("phase == .background { authentication.suspendForBackground() }"))
    #expect(root.contains("phase == .active { authentication.resumeFromBackground() }"))
    #expect(root.contains("authentication.canShowAccountData"))
}

@Test @MainActor func foregroundNeedsNewGenerationEvenForSameSDKAccount() throws {
    let identity = AuthIdentity(issuer: "https://test.example", subject: "user-test", sessionID: "session-test")
    let fence = AuthSessionFence(), before = fence.activate(identity)
    fence.suspend()
    #expect(throws: AuthenticationFailure.locked) { try fence.validate(before) }
    let after = fence.activate(identity)
    #expect(after.generation > before.generation)
    #expect(throws: AuthenticationFailure.changedSession) { try fence.validate(before) }
    try fence.validate(after)
}

@Test func appHasNoRuntimeFixtureLoaderSeedOrLocalInference() throws {
    let app = frontend.appendingPathComponent("App")
    let files = try FileManager.default.contentsOfDirectory(at: app, includingPropertiesForKeys: nil).filter { $0.pathExtension == "swift" }
    for file in files {
        let source = try String(contentsOf: file, encoding: .utf8)
        for forbidden in ["Bundle.module", "state.example", "DemoRequest", "/demo", "HomePresentation", "VisitPreparation", "VisitAnswerStore", "Try sample wording", "Load sample data"] {
            #expect(!source.contains(forbidden), "Runtime app must not contain \(forbidden) in \(file.lastPathComponent)")
        }
    }
    let manifest = try String(contentsOf: frontend.appendingPathComponent("Core/Package.swift"), encoding: .utf8)
    #expect(manifest.contains(".target(name: \"CompanionCore\")"))
    #expect(manifest.contains(".testTarget(name: \"CompanionCoreTests\""))
    #expect(manifest.contains("resources: [.copy(\"Fixtures\")]"))
}

@Test func actualAppRoutesBankManagementOnlyThroughVersionedCoordinator() throws {
    let root = try String(contentsOf: frontend.appendingPathComponent("App/LocalCompanionApp.swift"), encoding: .utf8)
    let settings = try String(contentsOf: frontend.appendingPathComponent("App/SettingsView.swift"), encoding: .utf8)
    let host = try String(contentsOf: frontend.appendingPathComponent("App/AppModel.swift"), encoding: .utf8)
    #expect(root.contains("bankAccountsPresented = true"))
    #expect(root.contains("NativeBankingPresentation(controller: model.banking)"))
    #expect(settings.contains("BankingScreen(controller: model.banking)"))
    #expect(!root.contains("model.plaid")); #expect(!settings.contains("model.plaid"))
    #expect(host.contains("plaid = legacyController"))
    #expect(host.contains("await banking.refresh(notify: false)"))
}
