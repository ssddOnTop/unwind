import Foundation

/// The one human-editable API address. Rebuild the app after changing this value.
/// Clerk issuer/publishable key remain separate authentication configuration.
public enum APIConfiguration {
    public static let baseURL = "http://127.0.0.1:8769"

    public static func resolve() throws -> URL { try APIOrigin.url(baseURL) }
}

/// Root origins only: no path prefix, credentials, query, fragment or URL aliases.
public enum APIOrigin {
    public static func url(_ value: String) throws -> URL {
        guard !value.isEmpty, value.unicodeScalars.allSatisfy({ $0.value > 32 && $0.value < 127 }),
              !value.contains("%"), !value.contains("\\"),
              var parts = URLComponents(string: value),
              let scheme = parts.scheme, ["http", "https"].contains(scheme),
              let host = parts.host, !host.isEmpty,
              host.split(separator: ".", omittingEmptySubsequences: false).allSatisfy({ label in
                  !label.isEmpty && label.count <= 63 && label.first != "-" && label.last != "-" &&
                  label.utf8.allSatisfy { (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 }
              }), host.count <= 253,
              scheme == "https" || ["127.0.0.1", "localhost"].contains(host.lowercased()),
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/",
              parts.port == nil || (1...65535).contains(parts.port!),
              !value.hasSuffix(":"), !value.hasSuffix(":/") else { throw ClientFailure.invalidEndpoint }
        parts.host = host.lowercased(); parts.path = ""
        guard let url = parts.url else { throw ClientFailure.invalidEndpoint }
        return url
    }
}
