import XCTest

/// Only presents the real installed SDK. Never signs up, enters credentials, requests an OTP,
/// calls an anonymous backend, creates tokens, seeds records or simulates a successful session.
final class AuthRuntimeTests: XCTestCase {
    @MainActor func testNativeClerkSignInAndNoAnonymousTabs() throws {
        continueAfterFailure = false
        let app = XCUIApplication(); app.launch()
        let identifier = app.textFields["clerk.auth.start.identifier"]
        XCTAssertTrue(identifier.waitForExistence(timeout: 45), "Official Clerk AuthView must load. Check Native API/dashboard configuration if it does not.")
        XCTAssertTrue(app.buttons["clerk.auth.start.continue"].exists)
        XCTAssertFalse(app.tabBars.buttons["Today"].exists)
        XCTAssertFalse(app.buttons["onboarding.skip"].exists)
        XCTAssertFalse(app.buttons["Load sample data"].exists)
        let value = identifier.value as? String ?? ""
        guard value.isEmpty || value == identifier.placeholderValue else {
            throw XCTSkip("An existing identifier is present; no screenshot or credential interaction is authorized")
        }
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "auth-native-clerk-sign-in-no-credentials"; screenshot.lifetime = .keepAlways; add(screenshot)
        app.terminate(); app.launch()
        XCTAssertTrue(app.textFields["clerk.auth.start.identifier"].waitForExistence(timeout: 30))
        XCTAssertFalse(app.tabBars.buttons["Today"].exists)
    }
}
