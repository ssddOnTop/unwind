import XCTest
import Foundation

final class RuntimeSmokeTests: XCTestCase {
    private var servicePort = 8000
    @MainActor private func configuredApp(readOnly: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--service-port", String(servicePort)] + (readOnly ? ["--read-only-preview"] : [])
        return app
    }
    @MainActor private func tap(_ label: String, app: XCUIApplication) {
        let button = app.buttons.matching(identifier: label).firstMatch
        for _ in 0..<14 {
            if button.exists && button.isHittable && button.isEnabled { button.tap(); return }
            app.swipeUp()
        }
        XCTFail("Button not hittable: \(label)")
    }
    @MainActor private func enterApp(_ app: XCUIApplication) {
        if app.buttons["onboarding.skip"].waitForExistence(timeout: 2) { app.buttons["onboarding.skip"].tap() }
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 15))
    }
    @MainActor private func settingsRoot(_ app: XCUIApplication) {
        app.tabBars.buttons["Settings"].tap()
        for _ in 0..<4 {
            if app.navigationBars["Settings"].exists { return }
            app.navigationBars.buttons.firstMatch.tap()
        }
    }
    @MainActor private func samples(_ app: XCUIApplication) {
        settingsRoot(app); tap("Sample data and tools", app: app)
    }
    @MainActor private func deletionPage(_ app: XCUIApplication) {
        settingsRoot(app); tap("Your data", app: app)
    }
    @MainActor private func capture(_ name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
    @MainActor private func state(after revision: Int? = nil, differentEpoch: String? = nil, synthesisCompleted: Bool = false) async throws -> [String: Any] {
        for _ in 0..<175 {
            let request = URLRequest(url: URL(string: "http://127.0.0.1:\(servicePort)/state")!, cachePolicy: .reloadIgnoringLocalCacheData)
            let (data, response) = try await URLSession.shared.data(for: request)
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
            let result = try JSONSerialization.jsonObject(with: data) as! [String: Any]
            let revisionReady = revision == nil || (result["revision"] as! Int) > revision!
            let epochReady = differentEpoch == nil || (result["data_epoch"] as! String) != differentEpoch!
            let complete = !synthesisCompleted || ((result["process"] as! [String: Any])["synthesis"] as! [String: Any])["status"] as? String == "completed"
            if revisionReady && epochReady && complete { return result }
            try await Task.sleep(for: .milliseconds(200))
        }
        throw NSError(domain: "RuntimeSmoke", code: 1, userInfo: [NSLocalizedDescriptionKey: "Timed out waiting for committed mutation"])
    }
    @MainActor private func pristineState() async throws -> [String: Any] {
        let snapshot = try await state()
        XCTAssertEqual((snapshot["observations"] as! [Any]).count, 0, "Requires an empty isolated synthetic database")
        XCTAssertEqual((snapshot["feedback"] as! [Any]).count, 0)
        let preferences = snapshot["preferences"] as! [String: Any]
        XCTAssertEqual(preferences["notes"] as? String, "")
        XCTAssertEqual(preferences["cloud_consent"] as? Bool, false)
        XCTAssertEqual(preferences["enabled_source_ids"] as? [String], ["demo"])
        for source in snapshot["sources"] as! [[String: Any]] where source["id"] as? String != "demo" {
            XCTAssertEqual(source["enabled"] as? Bool, false)
            XCTAssertNotEqual(source["consent"] as? String, "granted")
        }
        return snapshot
    }
    @MainActor private func settled(_ app: XCUIApplication) {
        let spinner = app.progressIndicators.firstMatch
        if spinner.exists { XCTAssertTrue(spinner.waitForNonExistence(timeout: 35)) }
    }
    @MainActor func testSyntheticUserJourney() async throws {
        continueAfterFailure = false
        let before = try await pristineState()
        let app = configuredApp(); app.launch()
        enterApp(app)
        capture("01-connected-empty", app: app)
        samples(app); tap("Load representative synthetic demo", app: app)
        XCTAssertTrue(app.buttons["Load synthetic demo"].waitForExistence(timeout: 5))
        app.buttons["Load synthetic demo"].tap(); settled(app)
        let seeded = try await state(after: before["revision"] as? Int)
        XCTAssertEqual((seeded["observations"] as! [Any]).count, 6)
        app.tabBars.buttons["Ledger"].tap()
        let card = app.buttons.containing(.staticText, identifier: "Recorded restaurant purchases").firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 10)); card.tap()
        XCTAssertTrue(app.staticTexts["These records may be incomplete."].waitForExistence(timeout: 5))
        capture("02-ledger-detail", app: app)
        tap("Give correctness or usefulness feedback", app: app)
        XCTAssertTrue(app.navigationBars["Optional feedback"].waitForExistence(timeout: 5))
        tap("Save feedback", app: app); settled(app)
        let feedbackState = try await state(after: seeded["revision"] as? Int)
        XCTAssertEqual((feedbackState["feedback"] as! [Any]).count, 1)
        app.navigationBars.buttons.firstMatch.tap()
        settingsRoot(app)
        tap("Refresh and edit preferences", app: app)
        XCTAssertTrue(app.switches["No kitchen access"].waitForExistence(timeout: 10))
        app.switches["No kitchen access"].coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertEqual(app.switches["No kitchen access"].value as? String, "0")
        capture("03-preferences", app: app)
        tap("Save complete preferences", app: app); settled(app)
        let changed = try await state(after: feedbackState["revision"] as? Int)
        XCTAssertEqual((changed["preferences"] as! [String: Any])["no_kitchen"] as? Bool, false)
        app.terminate(); app.launch()
        enterApp(app)
        settingsRoot(app); tap("Refresh and edit preferences", app: app)
        XCTAssertTrue(app.switches["No kitchen access"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.switches["No kitchen access"].value as? String, "0")
        app.buttons["Cancel"].tap()
        samples(app); tap("Run scripted demo synthesis", app: app); settled(app)
        let processed = try await state(after: changed["revision"] as? Int, synthesisCompleted: true)
        let synthesis = (processed["process"] as! [String: Any])["synthesis"] as! [String: Any]
        XCTAssertEqual(synthesis["mode"] as? String, "scripted")
        XCTAssertEqual(synthesis["status"] as? String, "completed")
        capture("04-scripted-process", app: app)
        app.tabBars.buttons["Ledger"].tap()
        let newCard = app.buttons.containing(.staticText, identifier: "Recorded restaurant purchases").firstMatch
        XCTAssertTrue(newCard.waitForExistence(timeout: 10)); newCard.tap()
        tap("Stop this scenario", app: app); settled(app)
        let suppressed = try await state(after: processed["revision"] as? Int)
        XCTAssertTrue(((suppressed["preferences"] as! [String: Any])["suppressed_scenario_ids"] as! [String]).contains("purchase_recurrence"))
        capture("05-suppressed", app: app)
        deletionPage(app); tap("Delete all application data", app: app)
        let deletion = app.sheets.buttons["Delete all application data"]
        if deletion.waitForExistence(timeout: 5) { deletion.tap() }
        else { app.buttons.matching(identifier: "Delete all application data").allElementsBoundByIndex.last!.tap() }
        settled(app)
        let deleted = try await state(differentEpoch: seeded["data_epoch"] as? String)
        XCTAssertNotEqual(deleted["data_epoch"] as? String, seeded["data_epoch"] as? String)
        XCTAssertEqual((deleted["observations"] as! [Any]).count, 0)
        XCTAssertEqual((deleted["feedback"] as! [Any]).count, 0)
        capture("06-deleted", app: app)
        // Leave a healthy, explicitly seeded synthetic demo for the user's preview.
        app.terminate(); app.launch()
        enterApp(app)
        samples(app); tap("Load representative synthetic demo", app: app); app.buttons["Load synthetic demo"].tap()
        _ = try await state(after: deleted["revision"] as? Int)
        app.terminate(); app.launch()
        XCTAssertTrue(app.staticTexts.matching(identifier: "Sample data").firstMatch.waitForExistence(timeout: 15))
        capture("07-preview", app: app)
    }
    @MainActor func testSuppressionDeletionAndPreview() async throws {
        continueAfterFailure = false
        let pristine = try await pristineState()
        let app = configuredApp(); app.launch()
        enterApp(app)
        samples(app); tap("Load representative synthetic demo", app: app)
        XCTAssertTrue(app.buttons["Load synthetic demo"].waitForExistence(timeout: 5))
        app.buttons["Load synthetic demo"].tap()
        let initial = try await state(after: pristine["revision"] as? Int)
        XCTAssertEqual((initial["observations"] as! [Any]).count, 6)
        capture("01-independent-synthetic-seed", app: app)
        app.tabBars.buttons["Ledger"].tap()
        app.buttons.containing(.staticText, identifier: "Recorded restaurant purchases").firstMatch.tap()
        tap("Stop this scenario", app: app)
        let stopped = try await state(after: initial["revision"] as? Int)
        XCTAssertTrue(((stopped["preferences"] as! [String: Any])["suppressed_scenario_ids"] as! [String]).contains("purchase_recurrence"))
        capture("05-suppressed", app: app)
        deletionPage(app); tap("Delete all application data", app: app)
        let deletion = app.sheets.buttons["Delete all application data"]
        XCTAssertTrue(deletion.waitForExistence(timeout: 5)); deletion.tap()
        let deleted = try await state(differentEpoch: initial["data_epoch"] as? String)
        XCTAssertEqual((deleted["observations"] as! [Any]).count, 0)
        XCTAssertEqual((deleted["feedback"] as! [Any]).count, 0)
        capture("06-deleted", app: app)
        app.terminate(); app.launch()
        enterApp(app)
        samples(app); tap("Load representative synthetic demo", app: app); app.buttons["Load synthetic demo"].tap()
        _ = try await state(after: deleted["revision"] as? Int)
        app.terminate(); app.launch()
        XCTAssertTrue(app.staticTexts.matching(identifier: "Sample data").firstMatch.waitForExistence(timeout: 15))
        capture("07-preview", app: app)
    }

    @MainActor private func safeReadOnlyState() async throws -> [String: Any] {
        let value = try await state()
        let observations = value["observations"] as! [[String: Any]]
        let sources = value["sources"] as! [[String: Any]]
        let preferences = value["preferences"] as! [String: Any]
        guard observations.allSatisfy({ $0["mode"] as? String == "fixture" && $0["source_id"] as? String == "demo" }),
              sources.filter({ $0["id"] as? String != "demo" }).allSatisfy({ $0["enabled"] as? Bool == false && $0["consent"] as? String != "granted" }),
              ["", "Synthetic demo preferences; keep suggestions free."].contains(preferences["notes"] as? String ?? "invalid"),
              (value["feedback"] as! [[String: Any]]).allSatisfy({ ($0["note"] as? String ?? "invalid") == "" }) else {
            throw XCTSkip("Read-only UX screenshots require known synthetic records and no private-source consent")
        }
        return value
    }
    @MainActor private func assertUnchanged(_ before: [String: Any]) async throws {
        let after = try await state()
        for key in ["data_epoch", "revision"] { XCTAssertEqual(String(describing: before[key]!), String(describing: after[key]!)) }
        for key in ["preferences", "observations", "feedback", "sources"] {
            XCTAssertEqual(try JSONSerialization.data(withJSONObject: before[key]!, options: .sortedKeys), try JSONSerialization.data(withJSONObject: after[key]!, options: .sortedKeys))
        }
    }
    @MainActor func testReadOnlyOnboardingSkipPersists() async throws {
        continueAfterFailure = false
        let before = try await safeReadOnlyState()
        let app = configuredApp(readOnly: true); app.launch()
        if !app.buttons["onboarding.skip"].waitForExistence(timeout: 3) {
            settingsRoot(app); tap("Review setup", app: app)
        }
        XCTAssertTrue(app.buttons["Use without an account"].waitForExistence(timeout: 5))
        capture("ux-01-account-free-setup", app: app)
        app.buttons["Use without an account"].tap()
        XCTAssertTrue(app.staticTexts["Choose what to connect"].waitForExistence(timeout: 5))
        app.buttons["onboarding.skip"].tap()
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 5))
        app.terminate(); app.launch()
        XCTAssertTrue(app.tabBars.buttons["Today"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["onboarding.skip"].exists)
        XCTAssertFalse(app.staticTexts["Local backend snapshot"].exists)
        XCTAssertFalse(app.staticTexts["Recorded restaurant purchases"].exists)
        capture("ux-02-home-after-relaunch", app: app)
        try await assertUnchanged(before)
    }
    @MainActor func testReadOnlyNavigationConnectionsAndPopovers() async throws {
        continueAfterFailure = false
        let before = try await safeReadOnlyState()
        let app = configuredApp(readOnly: true); app.launch(); enterApp(app)
        XCTAssertTrue(app.buttons["About Your observation"].waitForExistence(timeout: 10))
        app.buttons["About Your observation"].tap()
        XCTAssertTrue(app.staticTexts["Your observation"].waitForExistence(timeout: 5))
        app.buttons["Done"].tap()
        app.tabBars.buttons["Ledger"].tap()
        XCTAssertTrue(app.navigationBars["Ledger"].waitForExistence(timeout: 5))
        if !(before["ledger"] as! [Any]).isEmpty {
            XCTAssertTrue(app.buttons.containing(.staticText, identifier: "Recorded restaurant purchases").firstMatch.waitForExistence(timeout: 10))
        }
        app.tabBars.buttons["Today"].tap()
        XCTAssertFalse(app.staticTexts["Recorded restaurant purchases"].exists)
        settingsRoot(app)
        tap("info.Connections", app: app)
        XCTAssertTrue(app.buttons["info.dismiss.Connections"].waitForExistence(timeout: 5)); app.buttons["info.dismiss.Connections"].tap()
        tap("Connect bank", app: app)
        XCTAssertTrue(app.buttons["Connect synthetic bank data"].waitForExistence(timeout: 5))
        app.buttons["Cancel"].tap()
        XCTAssertTrue(app.buttons["Connect bank"].exists)
        tap("Connect Apple Health", app: app)
        XCTAssertTrue(app.buttons["Agree and continue to Apple Health"].waitForExistence(timeout: 5))
        app.buttons["Not now"].tap()
        XCTAssertTrue(app.buttons["Connect Apple Health"].waitForExistence(timeout: 5))
        capture("ux-03-settings-optional-connections", app: app)
        try await assertUnchanged(before)
    }

    @MainActor func testIsolatedOnboardingSkipPersists() async throws {
        servicePort = 8768
        try await testReadOnlyOnboardingSkipPersists()
    }
    @MainActor func testIsolatedConnectionsAndPopovers() async throws {
        servicePort = 8768
        try await testReadOnlyNavigationConnectionsAndPopovers()
    }
    @MainActor private func tapAny(_ labels: [String], app: XCUIApplication, timeout: TimeInterval = 4) -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            guard app.webViews.firstMatch.exists else { return false }
            for label in labels {
                // Scope native Link actions to its web content. Global 'Done' can
                // otherwise select the keyboard accessory, not Plaid's next step.
                let matches = app.webViews.firstMatch.descendants(matching: .any).matching(NSPredicate(format: "label ==[c] %@ OR identifier == %@", label, label))
                for button in matches.allElementsBoundByIndex where button.exists && button.isHittable && button.isEnabled { button.tap(); return true }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        return false
    }
    @MainActor private func finishNativeLink(_ app: XCUIApplication, prefix: String) throws {
        // Link is real SDK UI; no API-created public token or fake success callback.
        XCTAssertTrue(app.webViews.firstMatch.waitForExistence(timeout: 35), "Native Link must present its actual web content")
        capture(prefix + "-01-link-presented", app: app)
        _ = tapAny(["Continue without phone number", "Continue as guest", "Get started", "Continue"], app: app, timeout: 10)
        let search = app.textFields.matching(NSPredicate(format: "placeholderValue CONTAINS[c] 'Search' OR label CONTAINS[c] 'Search'")).firstMatch
        if search.waitForExistence(timeout: 6) { search.tap(); search.typeText("First Platypus Bank") }
        // Inspected AX hierarchy: search text has duplicate StaticText labels; the
        // actual grouped result is an Other element inside 'Institution List'.
        let list = app.otherElements["Institution List"]
        let group = list.otherElements.matching(NSPredicate(format: "label == %@", "First Platypus Bank Multiple available")).firstMatch
        XCTAssertTrue(group.waitForExistence(timeout: 8)); group.tap()
        capture(prefix + "-institution-group-opened", app: app)
        let plainInstitution = app.otherElements["Institution List"].descendants(matching: .any).matching(NSPredicate(format: "label == %@", "First Platypus Bank")).firstMatch
        if plainInstitution.waitForExistence(timeout: 3) && plainInstitution.isHittable { plainInstitution.tap() }
        // Search may group Sandbox variants. Select the explicitly non-OAuth variant if offered.
        let nonOAuth = app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS[c] 'non-oauth' OR label CONTAINS[c] 'non oauth'")).firstMatch
        if nonOAuth.waitForExistence(timeout: 3) && nonOAuth.isHittable { nonOAuth.tap() }
        let variant = app.buttons.matching(NSPredicate(format: "label CONTAINS[c] 'First Platypus' AND NOT label CONTAINS[c] 'oauth'")).firstMatch
        if variant.exists && variant.isHittable { variant.tap() }
        _ = tapAny(["Continue", "Continue to login", "Log in"], app: app, timeout: 4)
        capture(prefix + "-02-before-credentials", app: app)
        let username = app.textFields.matching(NSPredicate(format: "label CONTAINS[c] 'username' OR placeholderValue CONTAINS[c] 'username' OR label CONTAINS[c] 'User ID'")).firstMatch
        XCTAssertTrue(username.waitForExistence(timeout: 12), "Non-OAuth Sandbox username field must be available")
        username.tap(); username.typeText("user_good")
        let password = app.secureTextFields.firstMatch
        XCTAssertTrue(password.waitForExistence(timeout: 5)); password.tap(); password.typeText("pass_good")
        XCTAssertTrue(tapAny(["Submit", "Continue", "Log in", "Sign in"], app: app))
        for _ in 0..<8 {
            if !app.webViews.firstMatch.exists { break }
            _ = tapAny(["Continue without phone number", "Continue", "Allow", "Connect", "Select all", "Continue without saving", "Skip", "Done"], app: app, timeout: 5)
        }
        capture(prefix + "-03-after-link-flow", app: app)
        XCTAssertTrue(app.webViews.firstMatch.waitForNonExistence(timeout: 30), "Actual Link must return success and dismiss")
    }
    @MainActor func testNativeSandboxLinkAndConnection() async throws {
        servicePort = 8768; continueAfterFailure = false
        let before = try await safeReadOnlyState()
        XCTAssertTrue((before["observations"] as! [Any]).isEmpty)
        let app = configuredApp(); app.launch(); enterApp(app)
        settingsRoot(app); tap("Connect bank", app: app)
        XCTAssertTrue(app.buttons["Connect synthetic bank data"].waitForExistence(timeout: 5))
        app.buttons["Connect synthetic bank data"].tap()
        try finishNativeLink(app, prefix: "first")
        var connected = false
        for _ in 0..<35 {
            let current = try await state()
            let source = (current["sources"] as! [[String: Any]]).first { $0["id"] as? String == "plaid" }!
            if source["consent"] as? String == "granted", ["syncing", "ready"].contains(source["status"] as? String ?? "") || !(source["last_success_at"] is NSNull) {
                connected = true; break
            }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertTrue(connected, "Backend state must confirm real Link exchange; no UI-only success")
        var syncRecorded = false
        for _ in 0..<40 {
            let current = try await state()
            let source = (current["sources"] as! [[String: Any]]).first { $0["id"] as? String == "plaid" }!
            if !(source["last_success_at"] is NSNull) { syncRecorded = true; break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertTrue(syncRecorded, "A real Sandbox sync must succeed, including a truthful waiting/empty result")
        if app.buttons["Stop updating"].exists { app.buttons["Stop updating"].tap() }
        let beforeManual = try await state(); tap("Update bank records", app: app)
        let imported = try await state(after: beforeManual["revision"] as? Int)
        for observation in imported["observations"] as! [[String: Any]] where observation["source_id"] as? String == "plaid" {
            XCTAssertEqual(observation["mode"] as? String, "fixture")
            XCTAssertEqual(observation["household_attribution"] as? String, "unknown")
            XCTAssertEqual((observation["provenance"] as! [String: Any])["adapter"] as? String, "plaid_sandbox")
            if let purchase = observation["purchase"] as? [String: Any] { XCTAssertNil(purchase["amount"]); XCTAssertNil(purchase["currency"]) }
        }
        capture("native-04-connected", app: app)
        tap("Disconnect Bank", app: app)
        XCTAssertTrue(app.buttons["Disconnect bank"].waitForExistence(timeout: 5)); app.buttons["Disconnect bank"].tap()
        var disconnected = false
        for _ in 0..<35 {
            let current = try await state()
            let source = (current["sources"] as! [[String: Any]]).first { $0["id"] as? String == "plaid" }!
            if source["enabled"] as? Bool == false && source["consent"] as? String == "revoked" && source["status"] as? String != "error" { disconnected = true; break }
            try await Task.sleep(for: .seconds(1))
        }
        XCTAssertTrue(disconnected, "Disconnect must be confirmed by the isolated backend")
        capture("native-05-disconnected", app: app)
        tap("Connect bank", app: app); app.buttons["Connect synthetic bank data"].tap()
        try finishNativeLink(app, prefix: "reconnect")
        _ = try await state(after: before["revision"] as? Int)
        capture("native-06-reconnected", app: app)
        let connectedState = try await state()
        deletionPage(app); tap("Delete all application data", app: app)
        let delete = app.buttons.matching(identifier: "Delete all application data").allElementsBoundByIndex.last!
        delete.tap()
        let deleted = try await state(differentEpoch: connectedState["data_epoch"] as? String)
        XCTAssertEqual((deleted["observations"] as! [Any]).count, 0)
        XCTAssertFalse((deleted["preferences"] as! [String: Any])["cloud_consent"] as! Bool)
        capture("native-07-deleted-connected-item", app: app)
        app.terminate(); app.launch(); enterApp(app)
        samples(app); tap("Load representative synthetic demo", app: app); app.buttons["Load synthetic demo"].tap()
        let seeded = try await state(after: deleted["revision"] as? Int)
        XCTAssertEqual((seeded["observations"] as! [Any]).count, 6)
        app.tabBars.buttons["Today"].tap()
        capture("native-08-synthetic-preview", app: app)
    }

    @MainActor private func resetApprovedSandboxAndSeed() async throws -> [String: Any] {
        XCTAssertEqual(servicePort, 8768, "This helper is restricted to the explicitly approved isolated port")
        let current = try await state()
        XCTAssertTrue((current["observations"] as! [[String: Any]]).allSatisfy { $0["mode"] as? String == "fixture" })
        XCTAssertTrue((current["sources"] as! [[String: Any]]).filter { !["demo", "plaid"].contains($0["id"] as! String) }.allSatisfy { $0["enabled"] as? Bool == false && $0["consent"] as? String != "granted" })
        func mutation(_ path: String, _ method: String, _ body: [String: Any]) async throws -> [String: Any] {
            var request = URLRequest(url: URL(string: "http://127.0.0.1:8768" + path)!)
            request.httpMethod = method; request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await URLSession.shared.data(for: request)
            XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
            return try JSONSerialization.jsonObject(with: data) as! [String: Any]
        }
        let deleted = try await mutation("/data", "DELETE", ["schema_version": "1", "request_id": UUID().uuidString, "data_epoch": current["data_epoch"]!, "confirm": "delete_all"])
        return try await mutation("/demo", "POST", ["schema_version": "1", "request_id": UUID().uuidString, "data_epoch": deleted["data_epoch"]!, "action": "seed", "fixture_set": "representative-v1", "consent_demo": true])
    }
    @MainActor private func tapAbove(_ identifier: String, app: XCUIApplication) {
        let back = app.buttons["visit.backToQuestion"]
        XCTAssertTrue(back.waitForExistence(timeout: 5)); XCTAssertTrue(back.isEnabled); back.tap()
        capture("visit-after-return-control", app: app)
        let button = app.buttons[identifier]
        let visible = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == true AND hittable == true"), object: button)
        XCTAssertEqual(XCTWaiter.wait(for: [visible], timeout: 6), .completed)
        button.tap()
    }
    @MainActor func testZIsolatedVisitAnswersCopyAndScriptedProcessing() async throws {
        servicePort = 8768; continueAfterFailure = false
        let initial = try await resetApprovedSandboxAndSeed()
        XCTAssertEqual((initial["observations"] as! [Any]).count, 6)
        let app = configuredApp(); app.launch(); enterApp(app)
        XCTAssertTrue(app.buttons["experience.home.action"].waitForExistence(timeout: 10))
        app.buttons["experience.home.action"].tap()
        XCTAssertTrue(app.buttons["visit.expand"].waitForExistence(timeout: 5)); app.buttons["visit.expand"].tap()
        XCTAssertTrue(app.staticTexts["visit.generatedBrief"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.textViews["visit.brief"].exists, "Typing must not be required")
        capture("visit-01-generated-no-input", app: app)
        tap("visit.copy", app: app)
        XCTAssertTrue(app.staticTexts["visit.copyStatus"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["visit.copyStatus"].label.contains("not sent"))
        tapAbove("visit.clarification.yes", app: app)
        XCTAssertTrue(app.buttons["visit.clarification.change"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["visit.generatedBrief"].label.contains("mostly for me"))
        app.buttons["visit.clarification.change"].tap(); app.buttons["visit.clarification.no"].tap()
        XCTAssertTrue(app.staticTexts["visit.generatedBrief"].label.contains("not mostly for me"))
        capture("visit-02-no-removes-personal-inference", app: app)
        app.buttons["visit.clarification.change"].tap(); app.buttons["visit.clarification.notSure"].tap()
        XCTAssertTrue(app.staticTexts["visit.generatedBrief"].label.contains("not sure"))
        app.terminate(); app.launch(); enterApp(app)
        app.buttons["experience.home.action"].tap(); app.buttons["visit.expand"].tap()
        XCTAssertTrue(app.buttons["visit.clarification.change"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["visit.clarification.yes"].exists)
        XCTAssertTrue(app.staticTexts["visit.generatedBrief"].label.contains("not sure"))
        capture("visit-03-answer-persists-after-relaunch", app: app)
        let afterLocal = try await state()
        XCTAssertEqual(afterLocal["revision"] as? Int, initial["revision"] as? Int, "Local clarification/copy must not write backend evidence")
        app.buttons["Done"].tap()
        samples(app); tap("Run scripted demo synthesis", app: app)
        let processed = try await state(after: initial["revision"] as? Int, synthesisCompleted: true)
        let synthesis = (processed["process"] as! [String: Any])["synthesis"] as! [String: Any]
        XCTAssertEqual(synthesis["mode"] as? String, "scripted")
        XCTAssertFalse((processed["preferences"] as! [String: Any])["cloud_consent"] as! Bool)
        XCTAssertFalse(app.buttons["Create cloud suggestions"].isEnabled)
        app.tabBars.buttons["Today"].tap()
        capture("visit-04-synthetic-home-preview", app: app)
    }

}
