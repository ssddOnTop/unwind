import XCTest

/// Current-user acceptance only. No account creation, sign-out, reset, seed or backend API access.
final class SignedInAcceptanceTests: XCTestCase {
    @MainActor private func capture(_ name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
    @MainActor func testCurrentSessionHomeSettingsLedger() throws {
        continueAfterFailure = false
        let app = XCUIApplication(); app.activate()
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 35), "STOP: existing SDK session/admission needs human attention; never sign up or guess credentials")
        XCTAssertFalse(app.textFields["clerk.auth.start.identifier"].exists)
        app.tabBars.buttons["Today"].tap()
        XCTAssertTrue(app.buttons["experience.home.action"].waitForExistence(timeout: 15), "A backend experience must be rendered after admission")
        capture("signed-in-home-private", app: app)
        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        for _ in 0..<8 {
            let info = app.buttons["info.Connections"]
            if info.exists && info.isHittable { break }
            app.swipeDown()
        }
        XCTAssertTrue(app.buttons["info.Connections"].isHittable, "Inspect connection status before any Connect action")
        capture("signed-in-settings-private", app: app)
        // Captures only actual rendered state; never interprets an available Connect button as no Item.
        let ui = XCTAttachment(string: app.debugDescription)
        ui.name = "signed-in-settings-hierarchy-private"; ui.lifetime = .keepAlways; add(ui)
        app.tabBars.buttons["Ledger"].tap()
        XCTAssertTrue(app.navigationBars["Ledger"].waitForExistence(timeout: 5))
        capture("signed-in-ledger-private", app: app)
        app.tabBars.buttons["Today"].tap()
    }

    /// Execute at most once against the current account. Never creates/removes an Item.
    @MainActor func testExistingBankSingleManualUpdate() throws {
        continueAfterFailure = false
        let app = XCUIApplication(); app.activate()
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 35), "STOP for human sign-in if the active session is unavailable")
        app.tabBars.buttons["Settings"].tap()
        for _ in 0..<8 {
            if app.buttons["info.Connections"].exists && app.buttons["info.Connections"].isHittable { break }
            app.swipeDown()
        }
        XCTAssertTrue(app.buttons["Disconnect Bank"].exists, "Existing connection required; never create a second Item")
        XCTAssertTrue(app.staticTexts["Connected"].exists)
        XCTAssertFalse(app.buttons["Connect bank"].exists)
        let update = app.buttons["Update bank records"]
        for _ in 0..<5 {
            if update.exists && update.isHittable { break }; app.swipeUp()
        }
        XCTAssertTrue(update.isHittable && update.isEnabled)
        update.tap() // The only manual sync invocation in this test; normal bounded foreground polls may follow.
        let result = app.staticTexts["bank.update.result"]
        XCTAssertTrue(result.waitForExistence(timeout: 45), "No confirmed sync response; do not repeat the manual update")
        let deadline = Date().addingTimeInterval(100)
        while app.buttons["Stop updating"].exists && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(1))
        }
        capture("existing-bank-after-single-update-private", app: app)
        let status = result.value as? String ?? "unknown"
        let receipt = XCTAttachment(string: "server_sync_status=" + status)
        receipt.name = "existing-bank-server-status"; receipt.lifetime = .keepAlways; add(receipt)
        XCTAssertTrue(["waiting", "partial", "complete", "error"].contains(status))
        let details = app.buttons["info.Bank update"]
        if details.exists && details.isHittable {
            details.tap(); capture("existing-bank-update-counts-private", app: app)
            app.buttons["info.dismiss.Bank update"].tap()
        }
        app.tabBars.buttons["Today"].tap()
        capture("home-after-existing-bank-update-private", app: app)
        app.tabBars.buttons["Ledger"].tap()
        capture("ledger-after-existing-bank-update-private", app: app)
        app.tabBars.buttons["Today"].tap()
    }
}
