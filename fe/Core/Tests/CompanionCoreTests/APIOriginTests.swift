import Foundation
import Testing
@testable import CompanionCore

@Test func apiOriginAcceptsHTTPSAndLocalHTTPOnly() throws {
    for value in ["https://api.example.com", "https://api.example.com:8443/", "http://127.0.0.1:8769", "http://localhost:8769/"] {
        let url = try APIOrigin.url(value)
        #expect(url.path.isEmpty)
    }
    #expect(try APIConfiguration.resolve().absoluteString == APIConfiguration.baseURL)
}

@Test func apiOriginRejectsUnsafeOrNonrootConfiguration() {
    for value in ["", "api.example.com", "http://api.example.com", "ftp://api.example.com",
                  "https://user:pass@api.example.com", "https://api.example.com/api", "https://api.example.com//",
                  "https://api.example.com?", "https://api.example.com#", "https://api.example.com:0",
                  "https://api.example.com:65536", "https://api.example.com:", "https://api.example.com:/",
                  "https://*.example.com", "https://api.example.com\\other", "https://api%2eexample.com",
                  " https://api.example.com", "https://api.example.com\n", "https://", "http://127.0.0.2:8769"] {
        #expect(throws: ClientFailure.invalidEndpoint) { try APIOrigin.url(value) }
    }
}

@Test func appOriginHasNoLegacyPortOrPreferenceOverride() throws {
    let frontend = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let coordinator = try String(contentsOf: frontend.appendingPathComponent("App/ClerkSessionCoordinator.swift"), encoding: .utf8)
    #expect(coordinator.contains("APIConfiguration.resolve()"))
    #expect(coordinator.contains("lease: lease, origin: origin"))
    #expect(coordinator.contains("baseURL: origin, sessionValidator:"))
    #expect(coordinator.contains("deployment: apiOrigin.absoluteString"))
    #expect(!coordinator.contains("apiPort"))
    #expect(!coordinator.contains("LoopbackEndpoint"))
    let plist = try String(contentsOf: frontend.appendingPathComponent("App/Info.plist"), encoding: .utf8)
    #expect(!plist.contains("AuthenticatedAPIPort"))
    #expect(!plist.contains("COMPANION_AUTH_API_PORT"))
}
