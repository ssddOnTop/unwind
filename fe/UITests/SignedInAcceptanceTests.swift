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

/// Opt-in live operation. Never include this in unattended acceptance runs.
final class AuthorizedSingleBankSyncTests: XCTestCase {
    @MainActor private func capture(_ name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }

    @MainActor func testExplicitlyAuthorizedSingleSync() throws {
        let environment = ProcessInfo.processInfo.environment
        guard environment["COMPANION_AUTHORIZE_ONE_NATIVE_SYNC"] == "YES",
              let bank = environment["COMPANION_AUTHORIZED_BANK"], !bank.isEmpty else {
            throw XCTSkip("Requires fresh human authorization and an explicit connection display name")
        }
        continueAfterFailure = false
        let app = XCUIApplication(); app.activate()
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 15))
        defer { if app.tabBars.buttons["Today"].isHittable { app.tabBars.buttons["Today"].tap() } }
        app.tabBars.buttons["Settings"].tap()
        let back = app.navigationBars.buttons["Settings"]
        if back.exists && back.isHittable { back.tap() }
        let banks = app.buttons["settings.bankAccounts"]
        for _ in 0..<6 { if banks.isHittable { break }; app.swipeDown() }
        XCTAssertTrue(banks.isHittable); banks.tap()
        XCTAssertTrue(app.buttons["banking.add"].waitForExistence(timeout: 15))
        let row = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@ AND label ENDSWITH %@", bank + ",", "Connected")).firstMatch
        for _ in 0..<6 { if row.isHittable { break }; app.swipeUp() }
        XCTAssertTrue(row.isHittable); row.tap()
        // Scroll lazy rows into the accessibility tree without touching any toggles.
        app.swipeUp()
        // Read current selection; never toggle anything. Fail closed if it differs from consent.
        // SwiftUI exposes both a labeled wrapper and its nested UISwitch; count leaves only.
        let selected = app.switches.allElementsBoundByIndex.filter {
            $0.descendants(matching: .switch).count == 0 && ($0.value as? String) == "1"
        }
        XCTAssertEqual(selected.count, 2, "Expected the two previously user-enabled accounts")
        capture("pre-single-sync-selection-private", app: app)
        let update = app.buttons["Update imports: " + bank]
        for _ in 0..<6 { if update.isHittable { break }; app.swipeDown() }
        XCTAssertTrue(update.isHittable && update.isEnabled)
        update.tap() // Exactly one mutation tap. Never retry this method automatically.
        let result = app.descendants(matching: .any)["banking.action.result"]
        let returned = result.waitForExistence(timeout: 30)
        capture("single-sync-result-private", app: app)
        XCTAssertTrue(returned, "No bank action result within the bounded wait; do not repeat sync")
        app.swipeUp(); capture("post-single-sync-accounts-private", app: app)
        app.tabBars.buttons["Today"].tap()
        app.swipeDown()
        capture("post-single-sync-home-private", app: app)
        app.tabBars.buttons["Ledger"].tap()
        XCTAssertTrue(app.navigationBars["Inference history"].waitForExistence(timeout: 5))
        app.swipeDown()
        capture("post-single-sync-history-private", app: app)
    }
}
