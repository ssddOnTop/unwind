import Foundation

// Handwritten additive banking v1 types. Responses are deliberately Decodable, not
// Codable: Link credentials and inventory must not enter LocalSnapshot or the outbox.
public enum BankingStatus: String, Decodable, Sendable { case not_configured, not_connected, ready, attention }
public enum BankingEnvironment: String, Decodable, Sendable { case sandbox }
public enum BankingConnectionStatus: String, Decodable, Sendable { case connected, syncing, attention, disconnected }
public enum BankingAvailability: String, Decodable, Sendable { case available, missing, disconnected }
public enum BankingImportStatus: String, Decodable, Sendable { case disabled, not_synced, syncing, ready, error }
public enum BankingResultStatus: String, Decodable, Sendable {
    case link_ready, saved, complete, partial, cancelled, disconnected, removal_pending
}
public enum BankingErrorCode: String, Decodable, Sendable {
    case invalid_request, unsupported_version, epoch_mismatch, stale_banking, obsolete_selection
    case idempotency_conflict, consent_required, busy, not_found, not_configured, internal_error
}
public enum BankingFailure: Error, Equatable, Sendable {
    case invalidRequest, staleResponse, unexpectedHTTPStatus(Int)
}

public struct BankingVersion: Equatable, Sendable {
    public let data_epoch: String
    public let revision: Int
    public init(data_epoch: String, revision: Int) {
        self.data_epoch = data_epoch
        self.revision = revision
    }
    /// The caller supplies the current authenticated account's version, not a saved
    /// request's version. Account/session generation fencing remains the caller's job.
    public func accepts(_ incoming: BankingVersion) -> Bool {
        data_epoch == incoming.data_epoch && incoming.revision >= revision
    }
}

public struct BankingOverview: Decodable, Equatable, Sendable {
    public let schema_version: String
    public let data_epoch: String
    public let revision: Int
    public let generated_at: String
    public let environment: BankingEnvironment
    public let status: BankingStatus
    public let selection_notice: String
    public let connections: [BankingConnection]
    public let result: BankingResult?
    public var version: BankingVersion { BankingVersion(data_epoch: data_epoch, revision: revision) }

    public init(from decoder: Decoder) throws {
        let c = try BankingFields(decoder, "schema_version data_epoch revision generated_at environment status selection_notice connections result")
        schema_version = try c.string("schema_version", max: 1)
        try c.require(schema_version == "1")
        data_epoch = try c.id("data_epoch")
        revision = try c.integer("revision")
        generated_at = try c.timestamp("generated_at")
        environment = try c.value("environment")
        status = try c.value("status")
        selection_notice = try c.string("selection_notice", max: 500)
        connections = try c.array("connections")
        result = try c.optional("result")
    }
}

public struct BankingConnection: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let label: String
    public let institution_name: String?
    public let status: BankingConnectionStatus
    public let can_manage: Bool
    public let can_disconnect: Bool
    public let last_inventory_at: String?
    public let last_sync_at: String?
    public let accounts: [BankingAccount]
    public let groups: [BankingGroup]
    /// Only server-supplied text; never a provider/opaque identifier or invented bank.
    public var displayName: String { label.isEmpty ? (institution_name ?? "") : label }

    public init(from decoder: Decoder) throws {
        let c = try BankingFields(decoder, "id label institution_name status can_manage can_disconnect last_inventory_at last_sync_at accounts groups")
        id = try c.id("id")
        label = try c.string("label", max: 180)
        institution_name = try c.optionalString("institution_name", max: 180)
        status = try c.value("status")
        can_manage = try c.value("can_manage")
        can_disconnect = try c.value("can_disconnect")
        last_inventory_at = try c.optionalTimestamp("last_inventory_at")
        last_sync_at = try c.optionalTimestamp("last_sync_at")
        accounts = try c.array("accounts")
        groups = try c.array("groups")
    }
}

public struct BankingAccount: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let name: String
    public let official_name: String?
    public let mask: String?
    public let type: String
    public let subtype: String?
    public let availability: BankingAvailability
    public let enabled: Bool
    public let import_status: BankingImportStatus
    public let last_sync_at: String?
    public var displayName: String { name.isEmpty ? (official_name ?? "") : name }

    public init(from decoder: Decoder) throws {
        let c = try BankingFields(decoder, "id name official_name mask type subtype availability enabled import_status last_sync_at")
        id = try c.id("id")
        name = try c.string("name", max: 160)
        official_name = try c.optionalString("official_name", max: 240)
        mask = try c.optionalString("mask", max: 16)
        type = try c.string("type", max: 80)
        subtype = try c.optionalString("subtype", max: 100)
        availability = try c.value("availability")
        enabled = try c.value("enabled")
        import_status = try c.value("import_status")
        last_sync_at = try c.optionalTimestamp("last_sync_at")
    }
}

public struct BankingGroup: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let label: String
    public let account_ids: [String]
    public init(from decoder: Decoder) throws {
        let c = try BankingFields(decoder, "id label account_ids")
        id = try c.id("id")
        label = try c.string("label", max: 180)
        account_ids = try c.array("account_ids")
        try c.require(account_ids.allSatisfy(bankingID))
    }
}

public struct BankingResult: Decodable, Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let status: BankingResultStatus
    public let message: String
    public let link_session_id: String?
    public let link_token: String?
    public let expiration: String?
    public let added: Int?
    public let modified: Int?
    public let removed: Int?
    public var description: String { "BankingResult(\(status.rawValue), credentials redacted)" }
    public var debugDescription: String { description }

    public init(from decoder: Decoder) throws {
        let c = try BankingFields(decoder, "status message link_session_id link_token expiration added modified removed")
        status = try c.value("status")
        message = try c.string("message", max: 300)
        link_session_id = try c.optionalID("link_session_id")
        link_token = try c.optionalString("link_token", max: 4096)
        expiration = try c.optionalTimestamp("expiration")
        added = try c.optionalInteger("added")
        modified = try c.optionalInteger("modified")
        removed = try c.optionalInteger("removed")
    }
}

public struct BankingErrorEnvelope: Decodable, Equatable, Sendable {
    public let error: BankingAPIError
    public init(from decoder: Decoder) throws {
        error = try BankingFields(decoder, "error").value("error")
    }
}
public struct BankingAPIError: Error, Decodable, Equatable, Sendable {
    public let code: BankingErrorCode
    public let message: String
    public let retryable: Bool
    public let request_id: String?
    public let details: [BankingErrorDetail]
    public init(from decoder: Decoder) throws {
        let c = try BankingFields(decoder, "code message retryable request_id details")
        code = try c.value("code")
        message = try c.string("message", max: 500)
        retryable = try c.value("retryable")
        request_id = try c.optionalID("request_id")
        details = try c.array("details")
    }
}
public struct BankingErrorDetail: Decodable, Equatable, Sendable {
    public let field: String
    public let message: String
    public init(from decoder: Decoder) throws {
        let c = try BankingFields(decoder, "field message")
        field = try c.string("field", max: 240)
        message = try c.string("message", max: 240)
    }
}

public enum BankingResponse {
    /// Authentication errors belong to the separate auth envelope. The owner handles
    /// 401/403 before this decoder. A failure never becomes an empty overview.
    public static func decode(_ data: Data, statusCode: Int, current: BankingVersion? = nil) throws -> BankingOverview {
        switch statusCode {
        case 200:
            let overview = try Wire.decode(BankingOverview.self, data)
            if let current, !current.accepts(overview.version) { throw BankingFailure.staleResponse }
            return overview
        case 400, 404, 409, 413, 422, 500:
            throw try Wire.decode(BankingErrorEnvelope.self, data).error
        default:
            throw BankingFailure.unexpectedHTTPStatus(statusCode)
        }
    }
}

/// Strongly typed discriminants prevent update callbacks from accidentally exchanging
/// a public token. No action accepts provider callback metadata or a user ID.
public enum BankingAction: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    /// Emit only after explicit Sandbox consent.
    case addLink
    case exchange(linkSessionID: String, publicToken: String)
    case updateLink(connectionID: String)
    case reconcile(connectionID: String, linkSessionID: String)
    case cancelLink(linkSessionID: String)
    case sync(connectionID: String?)
    /// Emit only after confirming whole-bank disconnection.
    case disconnect(connectionID: String)

    public var name: String {
        switch self {
        case .addLink: "add_link"
        case .exchange: "exchange"
        case .updateLink: "update_link"
        case .reconcile: "reconcile"
        case .cancelLink: "cancel_link"
        case .sync: "sync"
        case .disconnect: "disconnect"
        }
    }
    public var description: String { "BankingAction(\(name), payload redacted)" }
    public var debugDescription: String { description }
}

/// View events retain the version actually rendered. Never replace it with a newer
/// revision to retry a stale selection. The owner assigns a stable request ID once.
public struct BankingEvent: Equatable, Sendable {
    public let action: BankingAction
    public let version: BankingVersion
    public init(action: BankingAction, version: BankingVersion) {
        self.action = action
        self.version = version
    }
    public func request(requestID: String) -> BankingRequest {
        BankingRequest(action: action, version: version, requestID: requestID)
    }
}

/// Encodable only, memory-only request; never put it or its bytes in PendingMutation.
public struct BankingRequest: Encodable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let action: BankingAction
    public let version: BankingVersion
    public let requestID: String
    public var description: String { "BankingRequest(\(action.name), payload redacted)" }
    public var debugDescription: String { description }
    public init(action: BankingAction, version: BankingVersion, requestID: String) {
        self.action = action
        self.version = version
        self.requestID = requestID
    }
    public func body() throws -> Data { try Wire.encode(self) }
    private enum CodingKeys: String, CodingKey {
        case schema_version, request_id, data_epoch, expected_revision, action, consent_sandbox
        case connection_id, link_session_id, public_token
    }
    public func encode(to encoder: Encoder) throws {
        guard bankingID(requestID), bankingID(version.data_epoch), version.revision >= 0 else {
            throw BankingFailure.invalidRequest
        }
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode("1", forKey: .schema_version)
        try c.encode(requestID, forKey: .request_id)
        try c.encode(version.data_epoch, forKey: .data_epoch)
        try c.encode(version.revision, forKey: .expected_revision)
        try c.encode(action.name, forKey: .action)
        func id(_ value: String, _ key: CodingKeys) throws {
            guard bankingID(value) else { throw BankingFailure.invalidRequest }
            try c.encode(value, forKey: key)
        }
        switch action {
        case .addLink:
            try c.encode(true, forKey: .consent_sandbox)
        case .exchange(let session, let token):
            try id(session, .link_session_id)
            guard token.hasPrefix("public-sandbox-"), (16...2048).contains(token.unicodeScalars.count) else {
                throw BankingFailure.invalidRequest
            }
            try c.encode(token, forKey: .public_token)
        case .updateLink(let connection), .disconnect(let connection):
            try id(connection, .connection_id)
        case .reconcile(let connection, let session):
            try id(connection, .connection_id)
            try id(session, .link_session_id)
        case .cancelLink(let session):
            try id(session, .link_session_id)
        case .sync(let connection):
            if let connection { try id(connection, .connection_id) }
            else { try c.encodeNil(forKey: .connection_id) }
        }
    }
}

// Same exact-key gate as Contract.swift, including required nullable properties.
// Validation stays wire-only: no inventory, grouping, or selection domain policy.
private struct BankingKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init(_ value: String) { stringValue = value }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}
private func bankingID(_ value: String) -> Bool {
    value.range(of: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$", options: .regularExpression) != nil
        && !value.contains("\n") && !value.contains("\r")
}
private struct BankingFields {
    let container: KeyedDecodingContainer<BankingKey>
    init(_ decoder: Decoder, _ fields: String) throws {
        container = try decoder.container(keyedBy: BankingKey.self)
        try require(Set(container.allKeys.map(\.stringValue)) == Set(fields.split(separator: " ").map(String.init)))
    }
    func require(_ condition: Bool) throws {
        guard condition else {
            throw DecodingError.dataCorrupted(.init(codingPath: container.codingPath, debugDescription: "Invalid banking contract value or keys"))
        }
    }
    func value<T: Decodable>(_ key: String) throws -> T { try container.decode(T.self, forKey: BankingKey(key)) }
    func optional<T: Decodable>(_ key: String) throws -> T? { try container.decodeIfPresent(T.self, forKey: BankingKey(key)) }
    func string(_ key: String, max: Int) throws -> String {
        let text: String = try value(key)
        try require(text.unicodeScalars.count <= max)
        return text
    }
    func optionalString(_ key: String, max: Int) throws -> String? {
        let text: String? = try optional(key)
        try require(text.map { $0.unicodeScalars.count <= max } ?? true)
        return text
    }
    func id(_ key: String) throws -> String {
        let text: String = try value(key)
        try require(bankingID(text))
        return text
    }
    func optionalID(_ key: String) throws -> String? {
        let text: String? = try optional(key)
        try require(text.map(bankingID) ?? true)
        return text
    }
    func integer(_ key: String) throws -> Int {
        let number: Int = try value(key)
        try require(number >= 0)
        return number
    }
    func optionalInteger(_ key: String) throws -> Int? {
        let number: Int? = try optional(key)
        try require(number.map { $0 >= 0 } ?? true)
        return number
    }
    func array<T: Decodable>(_ key: String) throws -> [T] {
        let values: [T] = try value(key)
        try require(values.count <= 1000)
        return values
    }
    func timestamp(_ key: String) throws -> String {
        let text: String = try value(key)
        try require(validDate(text))
        return text
    }
    func optionalTimestamp(_ key: String) throws -> String? {
        let text: String? = try optional(key)
        try require(text.map(validDate) ?? true)
        return text
    }
    private func validDate(_ text: String) -> Bool {
        guard text.range(of: "^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\\.[0-9]+)?([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$", options: .regularExpression) != nil,
              !text.contains("\n"), !text.contains("\r") else { return false }
        // ISO8601DateFormatter normalizes February 30 instead of rejecting it.
        let parts = text.prefix(10).split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3, (1...12).contains(parts[1]) else { return false }
        let leap = parts[0].isMultiple(of: 400) || (parts[0].isMultiple(of: 4) && !parts[0].isMultiple(of: 100))
        let days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        guard (1...days[parts[1] - 1]).contains(parts[2]) else { return false }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime]
        if parser.date(from: text.uppercased()) != nil { return true }
        parser.formatOptions.insert(.withFractionalSeconds)
        return parser.date(from: text.uppercased()) != nil
    }
}
