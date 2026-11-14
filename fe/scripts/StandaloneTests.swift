// Fallback runner for the existing CLT's real Swift Testing framework, not an iOS runtime.
import Foundation
import Testing
import Darwin

extension Bundle {
    static let module = Bundle(path: ProcessInfo.processInfo.environment["COMPANION_TEST_BUNDLE"]!)!
}
@main struct StandaloneTests {
    static func main() async {
        let status: CInt = await Testing.__swiftPMEntryPoint()
        exit(status)
    }
}
