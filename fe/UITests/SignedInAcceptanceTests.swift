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
            XCTAssertEqual(app.switches.count, 0)
            XCTAssertFalse(app.buttons["Select"].exists)
            XCTAssertTrue(app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Manage accounts: ")).count > 0)
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

    @MainActor func testExplicitlyAuthorizedAllBankSync() throws {
        guard ProcessInfo.processInfo.environment["COMPANION_AUTHORIZE_ALL_LINKED_SYNC"] == "YES" else {
            throw XCTSkip("Requires fresh authorization to refresh all currently provider-shared accounts")
        }
        continueAfterFailure = false
        let app = XCUIApplication(); app.activate()
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 20))
        app.tabBars.buttons["Settings"].tap()
        let back = app.navigationBars.buttons["Settings"]
        if back.exists && back.isHittable { back.tap() }
        let banks = app.buttons["settings.bankAccounts"]
        for _ in 0..<6 { if banks.isHittable { break }; app.swipeDown() }
        XCTAssertTrue(banks.isHittable); banks.tap()
        let update = app.buttons["banking.updateAll"]
        XCTAssertTrue(update.waitForExistence(timeout: 15))
        XCTAssertTrue(update.isHittable && update.isEnabled)
        XCTAssertEqual(app.buttons.matching(NSPredicate(format: "label ENDSWITH %@", "Connected")).count, 2)
        capture("before-all-linked-sync-private", app: app)
        update.tap() // One overall mutation only; never retry automatically.
        let result = app.descendants(matching: .any)["banking.action.result"]
        XCTAssertTrue(result.waitForExistence(timeout: 40), "Stop if pending/unavailable; do not repeat sync")
        capture("all-linked-sync-result-private", app: app)
        app.tabBars.buttons["Today"].tap()
        app.swipeDown()
        capture("all-linked-sync-home-private", app: app)
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
        let rows = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@ AND label ENDSWITH %@", bank + ",", "Connected"))
        XCTAssertEqual(rows.count, 1, "Bank display name must uniquely identify one connection; never choose the first ambiguous match")
        guard rows.count == 1 else { return }
        let row = rows.element(boundBy: 0)
        for _ in 0..<6 { if row.isHittable { break }; app.swipeUp() }
        XCTAssertTrue(row.isHittable); row.tap()
        // Read server-owned linked readiness; there are no local selection controls.
        let readiness = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Plaid-linked accounts for " + bank + ": "))
        for _ in 0..<6 { if readiness.count == 1 { break }; app.swipeUp() }
        XCTAssertEqual(readiness.count, 1)
        guard readiness.count == 1 else { return }
        let count = Int(readiness.element(boundBy: 0).label.components(separatedBy: ": ").last ?? "") ?? 0
        XCTAssertGreaterThan(count, 0, "Requires provider-linked accounts ready for the authorized sync")
        guard count > 0 else { return }
        XCTAssertEqual(app.switches.count, 0, "Bank accounts must not expose app selection toggles")
        capture("pre-single-sync-linked-readiness-private", app: app)
        let updates = app.buttons.matching(NSPredicate(format: "label == %@", "Update imports: " + bank))
        XCTAssertEqual(updates.count, 1, "Authorized bank action must be unique")
        guard updates.count == 1 else { return }
        let update = updates.element(boundBy: 0)
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
