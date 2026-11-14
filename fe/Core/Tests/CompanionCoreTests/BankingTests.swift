import Foundation
import Testing
@testable import CompanionCore

// Inline, test-only contract examples. Nothing is copied into the application bundle.
private let bankingSchemaExample = #"""
{"schema_version":"1","data_epoch":"banking-example-epoch","revision":0,"generated_at":"2026-11-14T12:00:00Z","environment":"sandbox","status":"not_connected","selection_notice":"Removing an account here stops future imports and excludes its saved records from new insights. Saved history stays in your ledger. This is a local setting, not bank-side access revocation.","connections":[],"result":null}
"""#
private let bankingInventoryExample = #"""
{
  "schema_version":"1","data_epoch":"banking-example-epoch","revision":12,
  "generated_at":"2026-11-14T12:00:00.123Z","environment":"sandbox","status":"ready",
  "selection_notice":"Keep saved history. Stop future imports. Exclude new insights.",
  "connections":[
    {"id":"connection-opaque","label":"Server bank label","institution_name":"Server institution","status":"connected",
     "can_manage":true,"can_disconnect":true,"last_inventory_at":"2026-11-14T12:00:00Z","last_sync_at":null,
     "accounts":[
       {"id":"account-opaque","name":"Server checking","official_name":"Server official name","mask":"0007","type":"depository","subtype":"checking","availability":"available","enabled":true,"import_status":"not_synced","last_sync_at":null},
       {"id":"account-other","name":"Server future account","official_name":null,"mask":null,"type":"future-type","subtype":"future-subtype","availability":"available","enabled":true,"import_status":"ready","last_sync_at":"2026-11-14T12:00:00+00:00"}
     ],
     "groups":[{"id":"group-opaque","label":"Server-defined group","account_ids":["account-other","account-opaque"]}]},
    {"id":"connection-retained","label":"Retained server bank","institution_name":null,"status":"disconnected",
     "can_manage":false,"can_disconnect":false,"last_inventory_at":null,"last_sync_at":null,
     "accounts":[{"id":"account-retained","name":"Retained account","official_name":null,"mask":"0012","type":"loan","subtype":null,"availability":"disconnected","enabled":false,"import_status":"disabled","last_sync_at":null}],"groups":[]}
  ],
  "result":{"status":"partial","message":"Server result message","link_session_id":null,"link_token":null,"expiration":null,"added":0,"modified":null,"removed":null}
}
"""#
private let bankingErrorExample = #"""
{"error":{"code":"stale_banking","message":"Refresh bank accounts.","retryable":false,"request_id":null,"details":[{"field":"expected_revision","message":"Refresh first."}]}}
"""#
private func bankingJSON(_ text: String = bankingInventoryExample) throws -> [String: Any] {
    try #require(JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any])
}
private func bankingData(_ object: Any) throws -> Data { try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) }
private func bankingOverview() throws -> BankingOverview { try Wire.decode(BankingOverview.self, Data(bankingInventoryExample.utf8)) }
private func bankingPayload(_ action: BankingAction, version: BankingVersion? = nil) throws -> [String: Any] {
    let request = BankingRequest(action: action, version: try version ?? bankingOverview().version, requestID: "select-1")
    return try #require(JSONSerialization.jsonObject(with: request.body()) as? [String: Any])
}
private func bankingConnectionJSON() throws -> [String: Any] {
    try #require((bankingJSON()["connections"] as? [[String: Any]])?.first)
}
private func bankingAccountJSON() throws -> [String: Any] {
    try #require((bankingConnectionJSON()["accounts"] as? [[String: Any]])?.first)
}

@Test func bankingFrozenSchemaExample() throws {
    let value = try Wire.decode(BankingOverview.self, Data(bankingSchemaExample.utf8))
    #expect(value.schema_version == "1")
    #expect(value.data_epoch == "banking-example-epoch")
    #expect(value.revision == 0)
    #expect(value.status == .not_connected)
    #expect(value.environment == .sandbox)
    #expect(value.connections.isEmpty)
    #expect(value.result == nil)
    #expect(value.selection_notice == (try bankingJSON(bankingSchemaExample)["selection_notice"] as? String))
}

@Test func bankingMultipleBanksPreserveServerInventoryAndUnknownSubtype() throws {
    let value = try bankingOverview()
    #expect(value.connections.count == 2)
    let bank = try #require(value.connections.first)
    #expect(bank.displayName == "Server bank label")
    #expect(bank.institution_name == "Server institution")
    #expect(bank.accounts[0].displayName == "Server checking")
    #expect(bank.accounts[0].official_name == "Server official name")
    #expect(bank.accounts[0].mask == "0007")
    #expect(bank.accounts[1].type == "future-type")
    #expect(bank.accounts[1].subtype == "future-subtype")
    #expect(bank.groups[0].label == "Server-defined group")
    #expect(bank.groups[0].account_ids == ["account-other", "account-opaque"])
    #expect(value.connections[1].status == .disconnected)
    #expect(value.connections[1].accounts[0].mask == "0012")
}

@Test func bankingPreservesServerOwnedLinkedAccountReadiness() throws {
    let bank = try bankingOverview().connections[0]
    #expect(bank.accounts.allSatisfy { $0.enabled })
    #expect(bank.accounts[0].import_status == .not_synced)
    #expect(bank.accounts[1].import_status == .ready)
}

@Test func bankingFallbackUsesOnlyServerTextNeverOpaqueIDsOrInventedNames() throws {
    var bank = try bankingConnectionJSON()
    bank["label"] = ""
    #expect(try Wire.decode(BankingConnection.self, bankingData(bank)).displayName == "Server institution")
    bank["institution_name"] = NSNull()
    #expect(try Wire.decode(BankingConnection.self, bankingData(bank)).displayName == "")
    var account = try bankingAccountJSON()
    account["name"] = ""
    #expect(try Wire.decode(BankingAccount.self, bankingData(account)).displayName == "Server official name")
    account["official_name"] = NSNull()
    #expect(try Wire.decode(BankingAccount.self, bankingData(account)).displayName == "")
}

@Test func bankingExplicitNullsAcceptedThroughoutInventoryAndResult() throws {
    let value = try bankingOverview()
    #expect(value.connections[0].last_sync_at == nil)
    #expect(value.connections[0].accounts[1].official_name == nil)
    #expect(value.connections[0].accounts[1].mask == nil)
    #expect(value.connections[1].institution_name == nil)
    #expect(value.connections[1].last_inventory_at == nil)
    #expect(value.connections[1].accounts[0].subtype == nil)
    #expect(value.result?.link_session_id == nil)
    #expect(value.result?.link_token == nil)
    #expect(value.result?.expiration == nil)
    #expect(value.result?.added == 0)
    #expect(value.result?.modified == nil)
    #expect(value.result?.removed == nil)
    let error = try Wire.decode(BankingErrorEnvelope.self, Data(bankingErrorExample.utf8))
    #expect(error.error.request_id == nil)
}

@Test func bankingEveryObjectRejectsMissingAndAdditionalProperties() throws {
    let root = try bankingJSON()
    let connection = try bankingConnectionJSON()
    let account = try bankingAccountJSON()
    let group = try #require((connection["groups"] as? [[String: Any]])?.first)
    let result = try #require(root["result"] as? [String: Any])
    let envelope = try bankingJSON(bankingErrorExample)
    let error = try #require(envelope["error"] as? [String: Any])
    let detail = try #require((error["details"] as? [[String: Any]])?.first)
    let cases: [([String: Any], (Data) throws -> Void)] = [
        (root, { _ = try Wire.decode(BankingOverview.self, $0) }),
        (connection, { _ = try Wire.decode(BankingConnection.self, $0) }),
        (account, { _ = try Wire.decode(BankingAccount.self, $0) }),
        (group, { _ = try Wire.decode(BankingGroup.self, $0) }),
        (result, { _ = try Wire.decode(BankingResult.self, $0) }),
        (envelope, { _ = try Wire.decode(BankingErrorEnvelope.self, $0) }),
        (error, { _ = try Wire.decode(BankingAPIError.self, $0) }),
        (detail, { _ = try Wire.decode(BankingErrorDetail.self, $0) })
    ]
    for (object, decode) in cases {
        for key in object.keys {
            var missing = object
            missing.removeValue(forKey: key)
            #expect(throws: (any Error).self) { try decode(bankingData(missing)) }
        }
        var extra = object
        extra["unexpected"] = "never accepted"
        #expect(throws: (any Error).self) { try decode(bankingData(extra)) }
    }
}

@Test func bankingRootRejectsNestedExtraProperties() throws {
    for target in ["connection", "account", "group", "result"] {
        var root = try bankingJSON()
        var connections = try #require(root["connections"] as? [[String: Any]])
        switch target {
        case "connection": connections[0]["extra"] = true
        case "account", "group":
            let key = target == "account" ? "accounts" : "groups"
            var objects = try #require(connections[0][key] as? [[String: Any]])
            objects[0]["extra"] = true
            connections[0][key] = objects
        default:
            var result = try #require(root["result"] as? [String: Any])
            result["extra"] = true
            root["result"] = result
        }
        root["connections"] = connections
        #expect(throws: (any Error).self) { try Wire.decode(BankingOverview.self, bankingData(root)) }
    }
}

@Test func bankingRejectsWrongTypesNotJustUnknownKeys() throws {
    let root = try bankingJSON()
    for (key, wrong) in [("revision", "12" as Any), ("revision", true), ("revision", 1.5),
                         ("schema_version", 1), ("connections", [:]), ("result", []), ("selection_notice", NSNull())] {
        var object = root
        object[key] = wrong
        #expect(throws: (any Error).self) { try Wire.decode(BankingOverview.self, bankingData(object)) }
    }
    for (key, wrong) in [("enabled", 1 as Any), ("mask", 7),
                         ("subtype", []), ("official_name", false), ("last_sync_at", 123)] {
        var object = try bankingAccountJSON()
        object[key] = wrong
        #expect(throws: (any Error).self) { try Wire.decode(BankingAccount.self, bankingData(object)) }
    }
}

@Test func bankingRejectsUnknownEnumsAndConstants() throws {
    for key in ["status", "environment", "schema_version"] {
        var object = try bankingJSON()
        object[key] = "unknown"
        #expect(throws: (any Error).self) { try Wire.decode(BankingOverview.self, bankingData(object)) }
    }
    var connection = try bankingConnectionJSON()
    connection["status"] = "unknown"
    #expect(throws: (any Error).self) { try Wire.decode(BankingConnection.self, bankingData(connection)) }
    for key in ["availability", "import_status"] {
        var account = try bankingAccountJSON()
        account[key] = "unknown"
        #expect(throws: (any Error).self) { try Wire.decode(BankingAccount.self, bankingData(account)) }
    }
    var result = try #require(bankingJSON()["result"] as? [String: Any])
    result["status"] = "unknown"
    #expect(throws: (any Error).self) { try Wire.decode(BankingResult.self, bankingData(result)) }
    var error = try #require(bankingJSON(bankingErrorExample)["error"] as? [String: Any])
    error["code"] = "provider_error"
    #expect(throws: (any Error).self) { try Wire.decode(BankingAPIError.self, bankingData(error)) }
}

@Test func bankingRejectsBoundsInvalidIDsAndMalformedDates() throws {
    for (key, wrong) in [("revision", -1 as Any), ("data_epoch", "bad id"),
                         ("generated_at", "2026-11-14"), ("generated_at", "not-a-date"),
                         ("generated_at", "2026-02-30T12:00:00Z"), ("generated_at", "2026-11-14T25:00:00Z"),
                         ("selection_notice", String(repeating: "x", count: 501)),
                         ("connections", Array(repeating: try bankingConnectionJSON(), count: 1001))] {
        var object = try bankingJSON()
        object[key] = wrong
        #expect(throws: (any Error).self) { try Wire.decode(BankingOverview.self, bankingData(object)) }
    }
    for (key, wrong) in [("mask", String(repeating: "0", count: 17)), ("name", String(repeating: "x", count: 161)),
                         ("id", "opaque\n"), ("subtype", String(repeating: "x", count: 101))] {
        var object = try bankingAccountJSON()
        object[key] = wrong
        #expect(throws: (any Error).self) { try Wire.decode(BankingAccount.self, bankingData(object)) }
    }
    var result = try #require(bankingJSON()["result"] as? [String: Any])
    result["added"] = -1
    #expect(throws: (any Error).self) { try Wire.decode(BankingResult.self, bankingData(result)) }
}

@Test func bankingDateTimeValidationPreservesValidWireStrings() throws {
    for timestamp in ["2024-02-29T23:59:59Z", "2026-11-14t12:00:00.123456z", "2026-11-14T12:00:00-05:00"] {
        var object = try bankingJSON()
        object["generated_at"] = timestamp
        #expect(try Wire.decode(BankingOverview.self, bankingData(object)).generated_at == timestamp)
    }
    for timestamp in ["2025-02-29T12:00:00Z", "2026-04-31T12:00:00Z", "2026-11-14T24:00:00Z", "2026-11-14T12:00:00+24:00"] {
        var object = try bankingJSON()
        object["generated_at"] = timestamp
        #expect(throws: (any Error).self) { try Wire.decode(BankingOverview.self, bankingData(object)) }
    }
}

@Test func bankingStringBoundsCountUnicodeScalarsNotGraphemeClusters() throws {
    var object = try bankingAccountJSON()
    object["mask"] = String(repeating: "e\u{301}", count: 8)
    _ = try Wire.decode(BankingAccount.self, bankingData(object))
    object["mask"] = String(repeating: "e\u{301}", count: 9)
    #expect(throws: (any Error).self) { try Wire.decode(BankingAccount.self, bankingData(object)) }
}

@Test func bankingEveryDocumentedResultStatusDecodes() throws {
    for status in ["link_ready", "saved", "complete", "partial", "cancelled", "disconnected", "removal_pending"] {
        var root = try bankingJSON()
        var result = try #require(root["result"] as? [String: Any])
        result["status"] = status
        root["result"] = result
        #expect(try Wire.decode(BankingOverview.self, bankingData(root)).result?.status.rawValue == status)
    }
}

@Test func bankingHTTPFailuresNeverProduceEmptySuccess() throws {
    let error = Data(bankingErrorExample.utf8)
    for status in [400, 404, 409, 413, 422, 500] {
        do {
            _ = try BankingResponse.decode(error, statusCode: status)
            Issue.record("An HTTP error became a successful overview")
        } catch let failure as BankingAPIError {
            #expect(failure.code == .stale_banking)
            #expect(failure.message == "Refresh bank accounts.")
        }
        #expect(throws: (any Error).self) {
            try BankingResponse.decode(Data(bankingSchemaExample.utf8), statusCode: status)
        }
    }
    for status in [201, 204, 302, 401, 403, 503] {
        #expect(throws: BankingFailure.unexpectedHTTPStatus(status)) {
            try BankingResponse.decode(error, statusCode: status)
        }
    }
    #expect(throws: (any Error).self) { try BankingResponse.decode(error, statusCode: 200) }
    #expect(throws: (any Error).self) { try BankingResponse.decode(Data("not json".utf8), statusCode: 200) }
}

@Test func bankingSuccessGuardRejectsStaleEpochAndRevision() throws {
    let data = Data(bankingInventoryExample.utf8)
    let current = BankingVersion(data_epoch: "banking-example-epoch", revision: 12)
    #expect(try BankingResponse.decode(data, statusCode: 200, current: current).revision == 12)
    #expect(try BankingResponse.decode(data, statusCode: 200, current: .init(data_epoch: current.data_epoch, revision: 11)).revision == 12)
    for version in [BankingVersion(data_epoch: "new-epoch", revision: 0), .init(data_epoch: current.data_epoch, revision: 13)] {
        #expect(throws: BankingFailure.staleResponse) { try BankingResponse.decode(data, statusCode: 200, current: version) }
    }
}

@Test func bankingFreshTimestampCannotOverrideRevisionGuard() throws {
    var object = try bankingJSON()
    object["generated_at"] = "2099-01-01T00:00:00Z"
    #expect(throws: BankingFailure.staleResponse) {
        try BankingResponse.decode(bankingData(object), statusCode: 200, current: .init(data_epoch: "banking-example-epoch", revision: 13))
    }
}

@Test func bankingManageMatchesMarkdownRequestExactly() throws {
    let object = try bankingPayload(.updateLink(connectionID: "connection-opaque"))
    let expected = try bankingJSON(#"{"schema_version":"1","request_id":"select-1","data_epoch":"banking-example-epoch","expected_revision":12,"action":"update_link","connection_id":"connection-opaque"}"#)
    #expect(object as NSDictionary == expected as NSDictionary)
}

@Test func bankingEveryRequestHasExactDiscriminatedFields() throws {
    let common: Set<String> = ["schema_version", "request_id", "data_epoch", "expected_revision", "action"]
    let cases: [(BankingAction, String, Set<String>)] = [
        (.addLink, "add_link", ["consent_sandbox"]),
        (.exchange(linkSessionID: "session", publicToken: "public-sandbox-test"), "exchange", ["link_session_id", "public_token"]),
        (.updateLink(connectionID: "connection"), "update_link", ["connection_id"]),
        (.reconcile(connectionID: "connection", linkSessionID: "session"), "reconcile", ["connection_id", "link_session_id"]),
        (.cancelLink(linkSessionID: "session"), "cancel_link", ["link_session_id"]),
        (.sync(connectionID: nil), "sync", ["connection_id"]),
        (.sync(connectionID: "connection"), "sync", ["connection_id"]),
        (.disconnect(connectionID: "connection"), "disconnect", ["connection_id"])
    ]
    for (action, name, fields) in cases {
        let payload = try bankingPayload(action)
        #expect(Set(payload.keys) == common.union(fields))
        #expect(payload["action"] as? String == name)
        #expect(payload["schema_version"] as? String == "1")
        #expect(payload["expected_revision"] as? Int == 12)
        #expect(payload["request_id"] as? String == "select-1")
        #expect(payload["data_epoch"] as? String == "banking-example-epoch")
    }
    #expect(try bankingPayload(.addLink)["consent_sandbox"] as? Bool == true)
}

@Test func bankingSyncAllEncodesExplicitNullConnection() throws {
    let all = try bankingPayload(.sync(connectionID: nil))
    #expect(all["connection_id"] is NSNull)
    #expect(try bankingPayload(.sync(connectionID: "one-bank"))["connection_id"] as? String == "one-bank")
}

@Test func bankingGroupsRemainReadOnlyServerMetadata() throws {
    let overview = try bankingOverview()
    let group = overview.connections[0].groups[0]
    #expect(group.account_ids == ["account-other", "account-opaque"])
    #expect(group.label == "Server-defined group")
    #expect(overview.connections[0].accounts.allSatisfy { $0.enabled })
}

@Test func bankingUpdateSuccessBuildsReconcileNeverExchange() throws {
    let update = try bankingPayload(.updateLink(connectionID: "existing-connection"))
    #expect(update["action"] as? String == "update_link")
    let complete = try bankingPayload(.reconcile(connectionID: "existing-connection", linkSessionID: "update-session"))
    #expect(complete["action"] as? String == "reconcile")
    #expect(complete["connection_id"] as? String == "existing-connection")
    #expect(complete["link_session_id"] as? String == "update-session")
    #expect(complete["public_token"] == nil)
    #expect(complete["accounts"] == nil && complete["institution"] == nil)
}

@Test func bankingRequestRetainsOriginalRevisionAndStableReplayBytes() throws {
    let version = try bankingOverview().version
    let event = BankingEvent(action: .updateLink(connectionID: "connection-opaque"), version: version)
    let request = event.request(requestID: "stable-retry")
    #expect(try request.body() == request.body())
    #expect(request.version == version)
    #expect(event.version.revision == 12)
    #expect(request.requestID == "stable-retry")
}

@Test func bankingRequestRejectsInvalidIDsSelectionAndTokens() throws {
    let invalid: [BankingAction] = [
        .updateLink(connectionID: ""), .disconnect(connectionID: "bad id"), .sync(connectionID: "\n"),
        .cancelLink(linkSessionID: String(repeating: "a", count: 161)),
        .reconcile(connectionID: "valid", linkSessionID: "bad/id"),
        .exchange(linkSessionID: "session", publicToken: "public-production-test"),
        .exchange(linkSessionID: "session", publicToken: "public-sandbox-"),
        .exchange(linkSessionID: "session", publicToken: "public-sandbox-" + String(repeating: "x", count: 2048)),
    ]
    for action in invalid { #expect(throws: BankingFailure.invalidRequest) { try bankingPayload(action) } }
    for version in [BankingVersion(data_epoch: "", revision: 0), .init(data_epoch: "epoch", revision: -1)] {
        #expect(throws: BankingFailure.invalidRequest) { try bankingPayload(.addLink, version: version) }
    }
    #expect(throws: BankingFailure.invalidRequest) {
        try BankingRequest(action: .addLink, version: .init(data_epoch: "epoch", revision: 0), requestID: "").body()
    }
}

@Test func bankingLinkCredentialsStayTypedAndDescriptionsRedactPayloads() throws {
    var root = try bankingJSON()
    var result = try #require(root["result"] as? [String: Any])
    result["status"] = "link_ready"
    result["link_session_id"] = "session-in-memory"
    result["link_token"] = "link-sandbox-test-only"
    result["expiration"] = "2026-11-14T13:00:00Z"
    root["result"] = result
    let overview = try Wire.decode(BankingOverview.self, bankingData(root))
    let decoded = try #require(overview.result)
    #expect(decoded.link_token == "link-sandbox-test-only")
    #expect(decoded.link_session_id == "session-in-memory")
    #expect(!String(reflecting: decoded).contains("link-sandbox-test-only"))
    let request = BankingRequest(action: .exchange(linkSessionID: "session", publicToken: "public-sandbox-test-only"), version: overview.version, requestID: "request")
    #expect(!String(reflecting: request).contains("public-sandbox-test-only"))
    #expect(!String(reflecting: request.action).contains("public-sandbox-test-only"))
}
