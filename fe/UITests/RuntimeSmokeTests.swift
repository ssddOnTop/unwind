import XCTest
import Foundation

final class RuntimeSmokeTests: XCTestCase {
    @MainActor private func tap(_ label: String, app: XCUIApplication) {
        let button = app.buttons.matching(identifier: label).firstMatch
        for _ in 0..<14 {
            if button.exists && button.isHittable { button.tap(); return }
            app.swipeUp()
        }
        XCTFail("Button not hittable: \(label)")
    }
    @MainActor private func capture(_ name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
    @MainActor private func state(after revision: Int? = nil, differentEpoch: String? = nil, synthesisCompleted: Bool = false) async throws -> [String: Any] {
        for _ in 0..<175 {
            let request = URLRequest(url: URL(string: "http://127.0.0.1:8000/state")!, cachePolicy: .reloadIgnoringLocalCacheData)
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
        let app = XCUIApplication(); app.launch()
        XCTAssertTrue(app.staticTexts["Local backend snapshot"].waitForExistence(timeout: 15), "Actual app must retrieve state over iOS URLSession")
        capture("01-connected-empty", app: app)
        tap("Load representative synthetic demo", app: app)
        XCTAssertTrue(app.buttons["Load synthetic demo"].waitForExistence(timeout: 5))
        app.buttons["Load synthetic demo"].tap(); settled(app)
        let seeded = try await state(after: before["revision"] as? Int)
        XCTAssertEqual((seeded["observations"] as! [Any]).count, 6)
        app.tabBars.buttons["Ledger"].tap()
        let card = app.buttons.containing(.staticText, identifier: "Recorded restaurant purchases").firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 10)); card.tap()
        XCTAssertTrue(app.staticTexts["Coverage: partial"].waitForExistence(timeout: 5))
        capture("02-ledger-detail", app: app)
        tap("Give correctness or usefulness feedback", app: app)
        XCTAssertTrue(app.navigationBars["Optional feedback"].waitForExistence(timeout: 5))
        tap("Save feedback", app: app); settled(app)
        let feedbackState = try await state(after: seeded["revision"] as? Int)
        XCTAssertEqual((feedbackState["feedback"] as! [Any]).count, 1)
        app.navigationBars.buttons.firstMatch.tap()
        app.tabBars.buttons["Settings"].tap()
        tap("Refresh and edit preferences", app: app)
        XCTAssertTrue(app.switches["No kitchen access"].waitForExistence(timeout: 10))
        app.switches["No kitchen access"].coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.5)).tap()
        XCTAssertEqual(app.switches["No kitchen access"].value as? String, "0")
        capture("03-preferences", app: app)
        tap("Save complete preferences", app: app); settled(app)
        let changed = try await state(after: feedbackState["revision"] as? Int)
        XCTAssertEqual((changed["preferences"] as! [String: Any])["no_kitchen"] as? Bool, false)
        app.terminate(); app.launch()
        XCTAssertTrue(app.staticTexts["Local backend snapshot"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Settings"].tap(); tap("Refresh and edit preferences", app: app)
        XCTAssertTrue(app.switches["No kitchen access"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.switches["No kitchen access"].value as? String, "0")
        app.buttons["Cancel"].tap()
        app.tabBars.buttons["Today"].tap(); tap("Run scripted demo synthesis", app: app); settled(app)
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
        app.tabBars.buttons["Settings"].tap(); tap("Delete all application data", app: app)
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
        XCTAssertTrue(app.staticTexts["Local backend snapshot"].waitForExistence(timeout: 15))
        tap("Load representative synthetic demo", app: app); app.buttons["Load synthetic demo"].tap()
        _ = try await state(after: deleted["revision"] as? Int)
        app.terminate(); app.launch()
        XCTAssertTrue(app.staticTexts["Contains synthetic fixtures—not personal history"].waitForExistence(timeout: 15))
        capture("07-preview", app: app)
    }
    @MainActor func testSuppressionDeletionAndPreview() async throws {
        continueAfterFailure = false
        let pristine = try await pristineState()
        let app = XCUIApplication(); app.launch()
        XCTAssertTrue(app.staticTexts["Local backend snapshot"].waitForExistence(timeout: 15))
        tap("Load representative synthetic demo", app: app)
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
        app.tabBars.buttons["Settings"].tap(); tap("Delete all application data", app: app)
        let deletion = app.sheets.buttons["Delete all application data"]
        XCTAssertTrue(deletion.waitForExistence(timeout: 5)); deletion.tap()
        let deleted = try await state(differentEpoch: initial["data_epoch"] as? String)
        XCTAssertEqual((deleted["observations"] as! [Any]).count, 0)
        XCTAssertEqual((deleted["feedback"] as! [Any]).count, 0)
        capture("06-deleted", app: app)
        app.terminate(); app.launch()
        XCTAssertTrue(app.staticTexts["Local backend snapshot"].waitForExistence(timeout: 15))
        tap("Load representative synthetic demo", app: app); app.buttons["Load synthetic demo"].tap()
        _ = try await state(after: deleted["revision"] as? Int)
        app.terminate(); app.launch()
        XCTAssertTrue(app.staticTexts["Contains synthetic fixtures—not personal history"].waitForExistence(timeout: 15))
        capture("07-preview", app: app)
    }

}
