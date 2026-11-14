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

/// Explicitly authorized presentation only: never continues, submits, or changes linked accounts.
final class AuthorizedManagePresentationTests: XCTestCase {
    /// Recovery only for an interrupted authorized exit; never opens a new provider session.
    @MainActor func testFinishAuthorizedExitConfirmation() throws {
        guard ProcessInfo.processInfo.environment["COMPANION_AUTHORIZE_FINISH_EXIT"] == "YES" else {
            throw XCTSkip("Only run after inspecting the interrupted provider exit confirmation")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        XCTAssertEqual(app.state, .runningForeground)
        let exit = app.buttons["Yes, exit"]
        XCTAssertTrue(exit.isHittable); exit.tap()
        let today = app.tabBars.buttons["Today"]
        XCTAssertTrue(today.waitForExistence(timeout: 10)); XCTAssertTrue(today.isHittable)
        let bank = XCTAttachment(screenshot: app.screenshot())
        bank.name = "confirmed-provider-cancel-private"; bank.lifetime = .keepAlways; add(bank)
        today.tap()
        let home = XCTAttachment(screenshot: app.screenshot())
        home.name = "confirmed-provider-cancel-home-private"; home.lifetime = .keepAlways; add(home)
    }
    @MainActor func testNativeManagePresentationAndCancel() throws {
        guard ProcessInfo.processInfo.environment["COMPANION_AUTHORIZE_MANAGE_CANCEL"] == "YES" else {
            throw XCTSkip("Requires fresh authorization to open existing Manage accounts and cancel")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        // Do not launch/activate over someone else's active provider flow.
        XCTAssertEqual(app.state, .runningForeground)
        XCTAssertTrue(app.tabBars.buttons["Today"].isHittable, "Stop if another sheet or Link flow is active")
        func capture(_ name: String) {
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
        }
        app.tabBars.buttons["Today"].tap()
        capture("manage-cancel-home-before-private")
        let info = app.buttons["info.Your observation"]
        if info.isHittable {
            info.tap(); capture("home-observation-info-private")
            let done = app.buttons["info.dismiss.Your observation"]
            XCTAssertTrue(done.waitForExistence(timeout: 5)); done.tap()
        }
        app.tabBars.buttons["Settings"].tap()
        let back = app.navigationBars.buttons["Settings"]
        if back.isHittable { back.tap() }
        let banks = app.buttons["settings.bankAccounts"]
        for _ in 0..<6 { if banks.isHittable { break }; app.swipeDown() }
        XCTAssertTrue(banks.isHittable)
        capture("manage-cancel-settings-private"); banks.tap()
        XCTAssertTrue(app.buttons["banking.add"].waitForExistence(timeout: 15))
        let connections = app.buttons.matching(NSPredicate(format: "label ENDSWITH %@", "Connected"))
        XCTAssertEqual(connections.count, 2)
        let row = connections.element(boundBy: 0)
        XCTAssertTrue(row.isHittable); row.tap()
        let manage = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Manage accounts: "))
        XCTAssertEqual(manage.count, 1, "Only the explicitly expanded bank may expose Manage")
        let action = manage.element(boundBy: 0)
        XCTAssertTrue(action.isHittable && action.isEnabled)
        XCTAssertEqual(app.switches.count, 0)
        capture("manage-cancel-bank-before-private")
        action.tap() // Exactly one presentation request. No retries, Continue, consent, or submission.
        let close = app.buttons.matching(NSPredicate(format: "label == %@ OR label == %@", "Close", "Exit"))
        XCTAssertTrue(close.firstMatch.waitForExistence(timeout: 30), "Stop for human inspection if provider exit is not discoverable")
        capture("native-manage-sdk-presented-private")
        let tree = XCTAttachment(string: app.debugDescription)
        tree.name = "native-manage-accessibility-private"; tree.lifetime = .keepAlways; add(tree)
        XCTAssertEqual(close.count, 1, "Never guess among provider actions")
        XCTAssertTrue(close.element(boundBy: 0).isHittable)
        close.element(boundBy: 0).tap()
        capture("native-manage-after-close-private")
        // Verified native SDK exit confirmation; this discards the unsubmitted flow only.
        let confirmExit = app.buttons["Yes, exit"]
        if confirmExit.waitForExistence(timeout: 5) {
            XCTAssertTrue(confirmExit.isHittable); confirmExit.tap()
        }
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.tabBars.buttons["Today"].isHittable)
        capture("manage-cancel-bank-after-private")
        XCTAssertEqual(connections.count, 2)
        app.tabBars.buttons["Today"].tap()
        capture("manage-cancel-home-after-private")
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
