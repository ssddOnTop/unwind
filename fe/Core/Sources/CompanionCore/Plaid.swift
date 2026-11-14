import Foundation

public enum PlaidFailure: Error, Equatable, Sendable {
    case staleWork, invalidSandboxResponse, expiredLinkToken, ambiguousExchange
}

/// Connection requests are intentionally NOT PendingMutation and never enter the disk outbox.
public enum PlaidAction: Equatable, Sendable {
    case linkToken, exchange(publicToken: String), sync, disconnect
    public var name: String {
        switch self { case .linkToken: "link_token"; case .exchange: "exchange"; case .sync: "sync"; case .disconnect: "disconnect" }
    }
    public func body(epoch: String, requestID: String) throws -> Data {
        switch self {
        case .linkToken:
            return try Wire.encode(PlaidLinkTokenRequest(schema_version: "1", request_id: requestID, data_epoch: epoch, action: name, consent_sandbox: true))
        case .exchange(let token):
            guard token.hasPrefix("public-sandbox-"), token.count >= 16, token.count <= 2048 else { throw PlaidFailure.invalidSandboxResponse }
            return try Wire.encode(PlaidExchangeRequest(schema_version: "1", request_id: requestID, data_epoch: epoch, action: name, public_token: token))
        case .sync:
            return try Wire.encode(PlaidSyncRequest(schema_version: "1", request_id: requestID, data_epoch: epoch, action: name))
        case .disconnect:
            return try Wire.encode(PlaidDisconnectRequest(schema_version: "1", request_id: requestID, data_epoch: epoch, action: name))
        }
    }
}

public enum PlaidResult: Equatable, Sendable {
    case linkToken(PlaidLinkTokenResponseResult)
    case connected
    case sync(PlaidSyncResponseResult)
    case disconnected(String)
}

public struct PlaidReply: Equatable, Sendable {
    public let requestID: String
    public let epoch: String
    public let state: State
    public let result: PlaidResult
    public static func decode(_ data: Data, action: PlaidAction, requestID: String, epoch: String, now: Date = Date()) throws -> PlaidReply {
        let reply: PlaidReply
        switch action {
        case .linkToken:
            let value = try Wire.decode(PlaidLinkTokenResponse.self, data)
            guard value.result.link_token.hasPrefix("link-sandbox-"), value.result.link_token.count <= 2048,
                  let expiration = date(value.result.expiration), expiration > now else { throw PlaidFailure.expiredLinkToken }
            reply = PlaidReply(requestID: value.request_id, epoch: value.data_epoch, state: value.state, result: .linkToken(value.result))
        case .exchange:
            let value = try Wire.decode(PlaidExchangeResponse.self, data)
            reply = PlaidReply(requestID: value.request_id, epoch: value.data_epoch, state: value.state, result: .connected)
        case .sync:
            let value = try Wire.decode(PlaidSyncResponse.self, data)
            guard [value.result.added, value.result.modified, value.result.removed, value.result.skipped].allSatisfy({ $0 >= 0 }),
                  value.result.retry_after_seconds.map({ (1...300).contains($0) }) ?? true else { throw PlaidFailure.invalidSandboxResponse }
            reply = PlaidReply(requestID: value.request_id, epoch: value.data_epoch, state: value.state, result: .sync(value.result))
        case .disconnect:
            let value = try Wire.decode(PlaidDisconnectResponse.self, data)
            reply = PlaidReply(requestID: value.request_id, epoch: value.data_epoch, state: value.state, result: .disconnected(value.result.status))
        }
        guard reply.requestID == requestID, reply.epoch == epoch, reply.state.data_epoch == epoch else { throw PlaidFailure.staleWork }
        return reply
    }
    public static func date(_ text: String) -> Date? {
        let parser = ISO8601DateFormatter()
        if let date = parser.date(from: text) { return date }
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return parser.date(from: text)
    }
}

/// Only decides transport timing. It never computes health/purchase interpretations.
public struct PlaidPollPlan: Equatable, Sendable {
    public private(set) var attempts = 0
    private var cancelled = false
    public init() {}
    public mutating func cancel() { cancelled = true }
    public mutating func nextDelay(result: PlaidSyncResponseResult, foreground: Bool, connected: Bool) -> Int? {
        let backoff = [2, 4, 8, 16, 30]
        guard !cancelled, foreground, connected, attempts < backoff.count,
              result.status == "waiting" || result.status == "partial" || (result.status == "error" && result.retry_after_seconds != nil) else { return nil }
        let delay = max(backoff[attempts], result.retry_after_seconds ?? 0)
        attempts += 1
        return delay
    }
}
