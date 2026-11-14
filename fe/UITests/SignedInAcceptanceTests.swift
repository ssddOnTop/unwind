import XCTest

/// Read-only navigation acceptance for an explicitly authorized signed-in simulator.
/// Requires the authenticated banking profile. Never connects, syncs, selects, deletes or signs out.
final class SignedInAcceptanceTests: XCTestCase {
    @MainActor private func capture(_ name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
    @MainActor func testCurrentSessionHomeSettingsLedger() throws {
        continueAfterFailure = false
        let app = XCUIApplication(); app.activate()
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 35), "STOP for human authentication; never create an account or guess credentials")
        app.tabBars.buttons["Today"].tap(); capture("authenticated-home-private", app: app)
        app.tabBars.buttons["Settings"].tap()
        // A preserved session may retain the Bank accounts navigation stack from a prior run.
        let backToSettings = app.navigationBars.buttons["Settings"]
        if backToSettings.exists && backToSettings.isHittable { backToSettings.tap() }
        let banks = app.buttons["settings.bankAccounts"]
        for _ in 0..<8 { if banks.exists && banks.isHittable { break }; app.swipeDown() }
        XCTAssertTrue(banks.isHittable)
        capture("authenticated-settings-private", app: app)
        banks.tap()
        // SwiftUI List is a CollectionView on current simulator runtimes, not Other.
        XCTAssertTrue(app.descendants(matching: .any)["banking.screen"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["banking.add"].waitForExistence(timeout: 20), "Enable banking backend profile before runtime acceptance")
        capture("server-bank-inventory-private", app: app)
        XCTAssertFalse(app.buttons["Update bank records"].exists)
        // Expand disclosure rows only. Never touch account toggles or action buttons.
        let connections = app.buttons.matching(NSPredicate(format: "label ENDSWITH %@", "Connected"))
        let count = connections.count
        for index in 0..<count {
            let row = connections.element(boundBy: index)
            for _ in 0..<6 { if row.isHittable { break }; app.swipeUp() }
            XCTAssertTrue(row.isHittable); row.tap()
            capture("bank-\(index + 1)-expanded-private", app: app)
            app.swipeUp()
            capture("bank-\(index + 1)-accounts-private", app: app)
            for _ in 0..<8 { if row.isHittable { break }; app.swipeDown() }
            XCTAssertTrue(row.isHittable); row.tap()
        }
        app.tabBars.buttons["Ledger"].tap()
        XCTAssertTrue(app.navigationBars["Inference history"].waitForExistence(timeout: 5)); capture("authenticated-ledger-private", app: app)
        app.tabBars.buttons["Today"].tap()
    }
}
