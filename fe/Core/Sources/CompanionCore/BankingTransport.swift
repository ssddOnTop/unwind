import Foundation

public enum BankingTransportFailure: Error, Equatable, Sendable {
    case profileUnavailable
    case staleAction
    case ambiguousExchange
    case invalidLinkSession
    case interrupted
}

/// Native callback context is memory-only and never inferred from provider metadata.
public struct BankingLinkContext: Sendable, Equatable {
    public enum Mode: Sendable, Equatable { case add; case update(connectionID: String) }
    public let mode: Mode
    public let sessionID: String
    public let epoch: String
    public init(mode: Mode, sessionID: String, epoch: String) {
        self.mode = mode; self.sessionID = sessionID; self.epoch = epoch
    }
    public func success(publicToken: String?, version: BankingVersion) throws -> BankingEvent {
        guard version.data_epoch == epoch else { throw BankingTransportFailure.staleAction }
        switch mode {
        case .add:
            guard let publicToken else { throw BankingTransportFailure.invalidLinkSession }
            return BankingEvent(action: .exchange(linkSessionID: sessionID, publicToken: publicToken), version: version)
        case .update(let connection):
            return BankingEvent(action: .reconcile(connectionID: connection, linkSessionID: sessionID), version: version)
        }
    }
}
