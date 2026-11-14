import Foundation

@main struct DecodeBackend {
    static func main() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let states = try Wire.decode([State].self, data)
        guard !states.isEmpty, states.allSatisfy({ $0.schema_version == "1" }) else { throw ClientFailure.invalidResponse }
        let report = ["backend_snapshots_decoded": states.count]
        let encoded = try JSONSerialization.data(withJSONObject: report, options: .sortedKeys)
        print(String(decoding: encoded, as: UTF8.self))
    }
}
