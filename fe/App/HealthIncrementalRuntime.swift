import Foundation
import HealthKit
import UIKit
import CryptoKit
import CompanionCore

@MainActor private final class HealthJournalCheckpoints: HealthBackgroundCheckpointing {
    let client: CompanionClient
    init(_ client: CompanionClient) { self.client = client }
    func load(scope: HealthBackgroundScope, type: HealthBackgroundType) throws -> HealthBackgroundCursor? {
        guard client.snapshot.state?.data_epoch == scope.epoch else { throw HealthBackgroundFailure.staleContext }
        return client.healthCheckpoint(type: type.rawValue).map { HealthBackgroundCursor(start: $0.start, anchor: $0.anchor, terminalPending: $0.terminalPending) }
    }
    func save(_ cursor: HealthBackgroundCursor, scope: HealthBackgroundScope, type: HealthBackgroundType) throws {
        try client.saveHealthCheckpoint(type: type.rawValue, checkpoint: HealthIncrementalCheckpoint(start: cursor.start, anchor: cursor.anchor, terminalPending: cursor.terminalPending), epoch: scope.epoch)
    }
}

/// One account instance owns its observer set. Never calls the complete-window importer.
@MainActor final class HealthIncrementalRuntime {
    private let client: CompanionClient
    private let account: String
    private let store = HKHealthStore()
    private var lease: HealthBackgroundLease?
    private var background: HealthOnlyTransport?
    private var coordinator: HealthBackgroundCoordinator<HealthKitBackgroundSource>?
    private var generation = UUID()
    private var unlockTask: Task<Void, Never>?
    private(set) var message = "Incremental Health has not been checked."
    private(set) var blocksSnapshot = false
    private var permitted = false
    private var capable: Bool {
        #if targetEnvironment(simulator)
        return false
        #else
        return Bundle.main.object(forInfoDictionaryKey: "HealthBackgroundCapability") as? String == "YES"
        #endif
    }
    init(client: CompanionClient, account: String) { self.client = client; self.account = account }
    func configure(_ lease: HealthBackgroundLease) {
        self.lease = lease; background = HealthOnlyTransport(base: lease.transport)
        client.setHealthSessionValidator { try lease.validate() }
        unlockTask = Task { [weak self] in
            for await _ in NotificationCenter.default.notifications(named: UIApplication.protectedDataDidBecomeAvailableNotification) {
                guard let self, !Task.isCancelled else { return }; await self.refresh()
            }
        }
    }
    func stop() {
        generation = UUID(); permitted = false; coordinator?.stop()
    }
    func close() { stop(); unlockTask?.cancel(); unlockTask = nil; lease?.stop(); lease = nil; background = nil }
    private func status() async throws -> HealthIncrementalStatus {
        guard let lease, let background, UIApplication.shared.isProtectedDataAvailable else { throw HealthBackgroundFailure.protectedData }
        try await lease.admit()
        return try await client.backgroundHealthStatus(using: background)
    }
    func refresh() async {
        let captured = generation
        do {
            let value = try await status()
            guard generation == captured else { return }
            blocksSnapshot = value.enabled || !value.snapshot_allowed || client.incrementalHealthHasState
            message = value.message
            guard value.enabled, value.schema_ready, value.source_allowed, value.baseline_supported else { stop(); return }
            guard client.snapshot.healthCollectionPaused != true else { message = "Collection is paused on this device. Explicitly connect Health to resume; queued deltas remain saved."; stop(); return }
            guard capable else { message = "Incremental Health requires the explicitly provisioned physical-device background capability. Existing records are preserved; snapshot fallback is disabled."; stop(); return }
            guard client.healthAuthorizationRequestedOnDevice else { message = "Use Connect Apple Health to request supported read permissions on this device."; stop(); return }
            try client.validateHealthJournal(value)
            let start = try parsedDate(value.baseline_start)
            for type in HealthBackgroundType.allCases where client.healthCheckpoint(type: type.rawValue) == nil {
                try client.saveHealthCheckpoint(type: type.rawValue, checkpoint: HealthIncrementalCheckpoint(start: start, anchor: nil, terminalPending: false), epoch: value.data_epoch)
            }
            permitted = true
            if coordinator == nil {
                coordinator = HealthBackgroundCoordinator(source: HealthKitBackgroundSource(store: store), checkpoints: HealthJournalCheckpoints(client), protectedData: { UIApplication.shared.isProtectedDataAvailable }, admit: { [weak self] scope, type, page in
                    guard let self else { throw HealthBackgroundFailure.staleContext }
                    return try await self.admit(scope: scope, type: type, page: page)
                })
            }
            coordinator?.updateContext(HealthBackgroundContext(scope: HealthBackgroundScope(accountID: account, epoch: value.data_epoch), authenticated: true, sourceEnabled: true, consentGranted: true, authorizationRequested: true))
            if let background { try await client.drainHealthDeltas(using: background) }
            coordinator?.retry()
        } catch {
            guard generation == captured else { return }
            stop(); blocksSnapshot = true
            message = "Incremental Health is paused: authenticate, unlock, or review the baseline/device cursor. Queued records are retained; no reset or snapshot fallback was performed."
        }
    }
    func connect() async throws {
        let current = try await status(); blocksSnapshot = current.enabled || !current.snapshot_allowed || client.incrementalHealthHasState
        guard blocksSnapshot else { return }
        guard capable, current.enabled, current.schema_ready, current.baseline_supported, current.source_allowed else { message = current.message; throw HealthBackgroundFailure.unavailable }
        try client.validateHealthJournal(current)
        let read: Set<HKObjectType> = [HKQuantityType(.stepCount),HKCategoryType(.sleepAnalysis),HKQuantityType(.heartRate),HKQuantityType(.restingHeartRate),HKObjectType.workoutType()]
        let token = generation
        try await store.requestAuthorization(toShare: [], read: read)
        guard generation == token else { throw HealthBackgroundFailure.staleContext }
        try client.recordHealthAuthorizationRequested()
        try client.setHealthCollectionPaused(false)
        await refresh()
    }
    private func admit(scope: HealthBackgroundScope, type: HealthBackgroundType, page: HealthBackgroundPage<HealthBackgroundChanges>) async throws -> HealthBackgroundAdmission {
        let token = generation
        guard permitted, scope.accountID == account, let background else { throw HealthBackgroundFailure.staleContext }
        let current = try await status()
        guard generation == token, permitted, current.data_epoch == scope.epoch, current.source_allowed, current.enabled, current.schema_ready, current.baseline_supported else { stop(); throw HealthBackgroundFailure.staleContext }
        try client.validateHealthJournal(current)
        let samples = try page.changes.samples.map { try normalize($0, type: type) }
        try client.admitHealthDelta(type: type.rawValue, epoch: scope.epoch, samples: samples, deletedUUIDs: page.changes.deleted.map { $0.uuid.uuidString.lowercased() }, anchor: page.nextAnchor, hasMore: page.hasMore)
        // Admission is durable before I/O. Transport failure retains the exact body and anchor obligation.
        try? await client.drainHealthDeltas(using: background)
        guard generation == token, permitted else { throw HealthBackgroundFailure.staleContext }
        return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
    }
    private func parsedDate(_ value: String) throws -> Date {
        let formatter = ISO8601DateFormatter()
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions.insert(.withFractionalSeconds)
        guard let date = formatter.date(from: value) else { throw HealthBackgroundFailure.invalidPage }; return date
    }
    private func normalize(_ sample: HKSample, type: HealthBackgroundType) throws -> IncrementalSample {
        let prefix = HealthNormalization.helperBundle, metadata = sample.metadata ?? [:]
        let synthetic = HealthNormalization.syntheticFlag(metadata[prefix + ".synthetic"])
        let dataset = metadata[prefix + ".datasetID"] as? String
        let recipe = metadata[prefix + ".recipe"] as? String
        let declaredZone = metadata[prefix + ".timezone"] as? String
        let revision = sample.sourceRevision
        let source = revision.source
        let stamp = ISO8601DateFormatter(); stamp.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var value: Double?, unit: String?, category: String?, activity: String?, duration: Double?, energy: Double?, distance: Double?, exclusion: String?
        if let quantity = sample as? HKQuantitySample {
            if type == .stepCount { value = quantity.quantity.doubleValue(for: .count()); unit = "count" }
            else { value = quantity.quantity.doubleValue(for: HKUnit.count().unitDivided(by: .minute())); unit = "bpm" }
        } else if let sleep = sample as? HKCategorySample {
            switch sleep.value {
            case HKCategoryValueSleepAnalysis.inBed.rawValue: category = "inBed"
            case HKCategoryValueSleepAnalysis.awake.rawValue: category = "awake"
            case HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue: category = "asleepUnspecified"
            case HKCategoryValueSleepAnalysis.asleepCore.rawValue: category = "asleepCore"
            case HKCategoryValueSleepAnalysis.asleepDeep.rawValue: category = "asleepDeep"
            case HKCategoryValueSleepAnalysis.asleepREM.rawValue: category = "asleepREM"
            default: exclusion = "unsupported_sleep_category"
            }
        } else if let workout = sample as? HKWorkout {
            switch workout.workoutActivityType {
            case .walking: activity = "walking"
            case .running: activity = "running"
            case .cycling: activity = "cycling"
            default: exclusion = "unsupported_workout_activity"
            }
            if exclusion == nil { duration = workout.duration; energy = workout.totalEnergyBurned?.doubleValue(for: .kilocalorie()); distance = workout.totalDistance?.doubleValue(for: .meter()) }
        } else { throw HealthBackgroundFailure.invalidPage }
        // Only a scoped device/source hash leaves this device, never localIdentifier or UDI.
        let deviceID = sample.device?.localIdentifier
        let sourceKnown = deviceID?.isEmpty == false
        if type == .stepCount && !synthetic && !sourceKnown { exclusion = "unsupported_step_source" }
        let streamMaterial = [account,source.bundleIdentifier,sample.device?.localIdentifier ?? "unknown",sample.device?.manufacturer ?? "unknown",sample.device?.model ?? "unknown"].joined(separator: "\u{0}")
        let stream = SHA256.hash(data: Data(streamMaterial.utf8)).map { String(format: "%02x", $0) }.joined()
        return IncrementalSample(hk_uuid: sample.uuid.uuidString.lowercased(), hk_type: type.rawValue, start_at: stamp.string(from: sample.startDate), end_at: stamp.string(from: sample.endDate), timezone: "UTC", value: value, unit: unit, category: category, workout_activity: activity, duration_seconds: duration, energy_kcal: energy, distance_m: distance, source_name: synthetic ? "Synthetic HealthKit helper" : String(source.name.prefix(80)), source_bundle: String(source.bundleIdentifier.prefix(180)), source_revision: String((revision.version ?? "unknown").prefix(80)), source_stream: type == .stepCount && !synthetic && sourceKnown && source.bundleIdentifier != HealthNormalization.helperBundle ? stream : nil, synthetic: synthetic, dataset_id: dataset, recipe: recipe, metadata_timezone: declaredZone ?? (sample.metadata?[HKMetadataKeyTimeZone] as? String).flatMap { TimeZone(identifier: $0) == nil ? nil : $0 }, excluded_reason: exclusion)
    }
}
