import Foundation

/// Normalization only: this does not grant authentication, consent or medical authority.
public enum HealthNormalization {
    public static let helperBundle = "org.localcompanion.test.healthseeder"
    public static let dataset = "six-month-pattern-demo-v1"
    public static let recipe = "six-month-pattern-demo-v1:1"
    public static func syntheticFlag(_ value: Any?) -> Bool {
        guard let number = value as? NSNumber else { return false }
        return number.doubleValue == 1
    }
    public static func provenance(bundle: String, synthetic: Bool, dataset: String?, recipe: String?, declaredTimezone: String? = "UTC") -> String? {
        if bundle == helperBundle {
            return synthetic && dataset == Self.dataset && recipe == Self.recipe && declaredTimezone == "UTC" ? "synthetic" : nil
        }
        // Unknown synthetic metadata is excluded, not relabeled as real wearable data.
        return !synthetic && dataset == nil && recipe == nil ? "realHealthKit" : nil
    }
    public static func batches(samples: [NormalizedHealthSample], epoch: String, start: Date, end: Date, collectionID: String = UUID().uuidString) throws -> [HealthIngestRequest] {
        guard epoch.range(of: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$", options: .regularExpression) != nil, UUID(uuidString: collectionID) != nil,
              end > start, end.timeIntervalSince(start) <= 187 * 86_400, samples.count <= 50_000,
              Set(samples.map(\.sample_id)).count == samples.count else { throw ClientFailure.invalidResponse }
        let count = max(1, (samples.count + 99) / 100), formatter = ISO8601DateFormatter()
        return (0..<count).map { index in
            HealthIngestRequest(schema_version: "1", request_id: UUID().uuidString, data_epoch: epoch,
                collection_id: collectionID, page_index: index, page_count: count,
                window_start: formatter.string(from: start), window_end: formatter.string(from: end),
                samples: Array(samples.dropFirst(index * 100).prefix(100)))
        }
    }
}
