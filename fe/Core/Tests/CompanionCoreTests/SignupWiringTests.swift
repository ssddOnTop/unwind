import Foundation
import Testing
@testable import CompanionCore

private var signupFrontend: URL {
    URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
}

/// Structural checks of SDK wiring, not provider/signup or device acceptance.
@Test func signupAndSignInShareOneSDKOwnedVerificationFlow() throws {
    let root = try String(contentsOf: signupFrontend.appendingPathComponent("App/LocalCompanionApp.swift"), encoding: .utf8)
    #expect(root.contains("AuthView(mode: .signInOrUp, isDismissible: false, onAuthComplete: authentication.authCompleted)"))
    #expect(root.components(separatedBy: "AuthView(mode:").count == 2)
    #expect(root.contains("} else if !clerk.isAuthFlowComplete {"))
    #expect(root.contains("\\(clerk.isAuthFlowComplete)"))
    #expect(root.contains(".accessibilityIdentifier(\"auth.nativeSignIn\")"))
    #expect(!root.contains("AuthView(mode: .signUp"))
    #expect(!root.contains("AuthView(mode: .signIn,"))
    let admission = try #require(root.range(of: "authentication.canShowAccountData, let model"))
    let onboarding = try #require(root.range(of: "OnboardingGate(model: model"))
    let main = try #require(root.range(of: "AppRoot(model: model"))
    #expect(admission.lowerBound < onboarding.lowerBound && onboarding.lowerBound < main.lowerBound)
}

@Test func pendingClerkPostAuthWorkCannotOpenBackendOrAccountCache() throws {
    let source = try String(contentsOf: signupFrontend.appendingPathComponent("App/ClerkSessionCoordinator.swift"), encoding: .utf8)
    #expect(source.contains("guard clerk.isLoaded, clerk.isAuthFlowComplete, let session = clerk.session, session.status == .active"))
    let admission = try #require(source.range(of: "try await transport.admit()"))
    let cache = try #require(source.range(of: "let storage = try cache.activate(identity)"))
    #expect(admission.lowerBound < cache.lowerBound)
    #expect(source.contains("func authCompleted() { managingAccount = false; activateDeliberately() }"))
}
