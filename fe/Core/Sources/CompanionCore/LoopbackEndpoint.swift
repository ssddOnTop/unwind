import Foundation

/// Only the app's DEBUG simulator branch enables this parser. Release ignores overrides.
public enum LoopbackEndpoint {
    public static func port(arguments: [String], environment: [String: String], allowOverride: Bool) throws -> Int {
        guard allowOverride else { return 8000 }
        let explicit: String?
        if let index = arguments.firstIndex(of: "--service-port") {
            guard index + 1 < arguments.count else { throw ClientFailure.invalidEndpoint }
            explicit = arguments[index + 1]
        } else { explicit = environment["LOCAL_COMPANION_TEST_PORT"] }
        guard let explicit else { return 8000 }
        guard !explicit.isEmpty, explicit.utf8.allSatisfy({ (48...57).contains($0) }),
              let value = Int(explicit), (1...65535).contains(value) else { throw ClientFailure.invalidEndpoint }
        return value
    }
    public static func url(port: Int) throws -> URL {
        guard (1...65535).contains(port), let url = URL(string: "http://127.0.0.1:\(port)") else { throw ClientFailure.invalidEndpoint }
        return url
    }
}
