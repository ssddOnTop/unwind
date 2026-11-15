import Foundation
import CryptoKit

/// Apple documentation verified 2026-11-14 (official DocC JSON, not simulator evidence):
/// https://developer.apple.com/documentation/healthkit/hkobserverquery
/// https://developer.apple.com/documentation/healthkit/hkanchoredobjectquery
/// https://developer.apple.com/documentation/healthkit/hkobserverquerycompletionhandler
/// https://developer.apple.com/documentation/healthkit/protecting-user-privacy
/// https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.healthkit.background-delivery
/// No timer promises: HealthKit delivery is opportunistic, not a nightly scheduler.
/// The owner must supply an authenticated context and a durable typed-outbox admission.
struct HealthBackgroundScope: Codable, Equatable, Sendable {
    let accountID: String
    let epoch: String
}

struct HealthBackgroundContext: Equatable, Sendable {
    let scope: HealthBackgroundScope
    let authenticated: Bool
    let sourceEnabled: Bool
    let consentGranted: Bool
    /// Foreground authorization was requested, NOT proof of HealthKit read access.
    let authorizationRequested: Bool
    var permitted: Bool {
        authenticated && sourceEnabled && consentGranted && authorizationRequested
            && !scope.accountID.isEmpty && !scope.epoch.isEmpty
    }
}

enum HealthBackgroundType: String, Codable, CaseIterable, Sendable {
    case stepCount, sleepAnalysis, heartRate, restingHeartRate, workout
}

enum HealthBackgroundFailure: Error, Equatable {
    case incrementalTransportUnavailable, staleContext, invalidCheckpoint, invalidPage
    case protectedData, unavailable, queryFailed, deliveryFailed, timedOut
}

/// Safe for callbacks on HealthKit's background queue; completion is always exactly once.
final class HealthBackgroundCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private var callback: (() -> Void)?
    init(_ callback: @escaping () -> Void) { self.callback = callback }
    func finish() {
        lock.lock(); let value = callback; callback = nil; lock.unlock()
        value?()
    }
}

struct HealthBackgroundCursor: Codable, Equatable, Sendable {
    /// Fixed predicate boundary. Never move a predicate while reusing its anchor.
    let start: Date
    let anchor: Data?
    /// A durably admitted hasMore page still needs a terminal admission, even after relaunch.
    /// This is not a statement about OS read permission or completeness of accessible data.
    let terminalPending: Bool

    init(start: Date, anchor: Data?, terminalPending: Bool = false) {
        self.start = start; self.anchor = anchor; self.terminalPending = terminalPending
    }
    private enum CodingKeys: String, CodingKey { case start, anchor, terminalPending }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        start = try values.decode(Date.self, forKey: .start)
        anchor = try values.decodeIfPresent(Data.self, forKey: .anchor)
        // Old checkpoints cannot establish an obligation merely from a nonnil anchor.
        terminalPending = try values.decodeIfPresent(Bool.self, forKey: .terminalPending) ?? false
        guard !terminalPending || anchor != nil else { throw HealthBackgroundFailure.invalidCheckpoint }
    }
}

struct HealthBackgroundPage<Changes> {
    let changes: Changes
    let nextAnchor: Data
    /// Count includes BOTH insertions and deletions, before any normalization/filtering.
    let changeCount: Int
    let hasMore: Bool
}

struct HealthBackgroundAdmission: Equatable, Sendable {
    let scope: HealthBackgroundScope
    let type: HealthBackgroundType
    let anchor: Data
}

@MainActor protocol HealthBackgroundSource: AnyObject {
    associatedtype Changes
    func start(_ update: @escaping @MainActor (HealthBackgroundType, HealthBackgroundCompletion, Bool) -> Void)
    func stop()
    func cancel(_ type: HealthBackgroundType)
    func page(type: HealthBackgroundType, cursor: HealthBackgroundCursor) async throws -> HealthBackgroundPage<Changes>
}

@MainActor protocol HealthBackgroundCheckpointing {
    func load(scope: HealthBackgroundScope, type: HealthBackgroundType) throws -> HealthBackgroundCursor?
    func save(_ cursor: HealthBackgroundCursor, scope: HealthBackgroundScope, type: HealthBackgroundType) throws
}

/// Checkpoints only, never an alternate ingestion database or an outbox of raw Health data.
/// Place under the account's protected storage directory; delete that directory on erasure.
@MainActor final class HealthBackgroundFileCheckpoints: HealthBackgroundCheckpointing {
    private struct Envelope: Codable {
        let version: Int
        let scope: HealthBackgroundScope
        let type: HealthBackgroundType
        let cursor: HealthBackgroundCursor
    }
    private let directory: URL
    init(directory: URL) { self.directory = directory }
    private func file(_ scope: HealthBackgroundScope, _ type: HealthBackgroundType) throws -> URL {
        let key = try JSONEncoder().encode([scope.accountID, scope.epoch, type.rawValue])
        let name = SHA256.hash(data: key).map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent(name + ".json")
    }
    func load(scope: HealthBackgroundScope, type: HealthBackgroundType) throws -> HealthBackgroundCursor? {
        let url = try file(scope, type)
        // Only ENOENT is absence. Locked, corrupt or inaccessible storage must fail closed.
        let data: Data
        do { data = try Data(contentsOf: url) }
        catch let error as NSError where error.domain == NSCocoaErrorDomain && error.code == NSFileReadNoSuchFileError { return nil }
        let value = try JSONDecoder().decode(Envelope.self, from: data)
        guard value.version == 1, value.scope == scope, value.type == type else { throw HealthBackgroundFailure.invalidCheckpoint }
        return value.cursor
    }
    func save(_ cursor: HealthBackgroundCursor, scope: HealthBackgroundScope, type: HealthBackgroundType) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        var folder = directory
        var flags = URLResourceValues(); flags.isExcludedFromBackup = true
        try folder.setResourceValues(flags)
        #if os(iOS)
        try fm.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: directory.path)
        #endif
        let data = try JSONEncoder().encode(Envelope(version: 1, scope: scope, type: type, cursor: cursor))
        let url = try file(scope, type)
        #if os(iOS)
        try data.write(to: url, options: [.atomic, .completeFileProtection])
        #else
        try data.write(to: url, options: .atomic)
        #endif
    }
}

@MainActor final class HealthBackgroundCoordinator<Source: HealthBackgroundSource> {
    /// MUST use the existing typed PendingMutation/CompanionClient outbox, never direct SQL/HTTP.
    /// Return only once ALL additions and explicit tombstones are durably admitted for this
    /// account/epoch. Replays after checkpoint failure must be idempotent. The owner must fence
    /// its own writes across suspension too; checking a receipt cannot undo a stale upload.
    /// Outbox expiry/discard MUST invalidate the corresponding checkpoints or retain a
    /// replay obligation. Otherwise durable admission followed by queue loss loses changes.
    /// Existing /health/v1/samples is a complete-scope snapshot: NEVER route a delta there.
    typealias Admit = @MainActor (HealthBackgroundScope, HealthBackgroundType, HealthBackgroundPage<Source.Changes>) async throws -> HealthBackgroundAdmission
    enum Status: Equatable {
        case stopped, observing, waitingForProtectedData, noReadableChanges
        case admitted, retryRequired, transportUnavailable
    }
    private(set) var status: Status = .stopped
    private(set) var typeStatus: [HealthBackgroundType: Status] = [:]
    private let source: Source
    private let checkpoints: any HealthBackgroundCheckpointing
    private let protectedData: @MainActor () -> Bool
    private let admit: Admit
    private let now: @MainActor () -> Date
    private let timeoutNanoseconds: UInt64
    private var context: HealthBackgroundContext?
    private var generation = UUID()
    private var flights: [HealthBackgroundType: UUID] = [:]
    private var tasks: [HealthBackgroundType: Task<Void, Never>] = [:]
    private var deadlines: [HealthBackgroundType: Task<Void, Never>] = [:]
    private var completions: [HealthBackgroundType: [HealthBackgroundCompletion]] = [:]
    private var dirty = Set<HealthBackgroundType>()

    init(source: Source, checkpoints: any HealthBackgroundCheckpointing,
         protectedData: @escaping @MainActor () -> Bool,
         now: @escaping @MainActor () -> Date = { Date() },
         timeoutNanoseconds: UInt64 = 20_000_000_000,
         admit: @escaping Admit = { _, _, _ in throw HealthBackgroundFailure.incrementalTransportUnavailable }) {
        self.source = source; self.checkpoints = checkpoints; self.protectedData = protectedData
        self.now = now; self.timeoutNanoseconds = timeoutNanoseconds; self.admit = admit
    }

    /// Call on login/admission, consent changes and epoch changes, not only scene activation.
    func updateContext(_ next: HealthBackgroundContext?) {
        guard next != context else { return }
        stop()
        guard let next, next.permitted else { return }
        context = next; status = .observing
        let token = generation
        source.start { [weak self] type, completion, failed in
            guard let self, self.generation == token else { completion.finish(); return }
            if failed { self.status = .retryRequired; self.typeStatus[type] = .retryRequired; completion.finish(); return }
            self.wake(type, completion: completion)
        }
        retry() // Reconcile after relaunch/unlock even if a prior delivery was acknowledged.
    }

    /// Must run BEFORE logout, reset, source disconnect or auth/service admission suspension.
    func stop() {
        generation = UUID(); context = nil
        tasks.values.forEach { $0.cancel() }; deadlines.values.forEach { $0.cancel() }
        tasks.removeAll(); deadlines.removeAll(); flights.removeAll(); dirty.removeAll()
        source.stop()
        let pending = completions.values.flatMap { $0 }; completions.removeAll()
        pending.forEach { $0.finish() }
        status = .stopped; typeStatus.removeAll()
    }

    /// Owner calls on protected-data availability, foreground admission and outbox recovery.
    /// No tight retry loop or assumption that iOS will deliver another background wake.
    func retry() {
        guard context?.permitted == true else { return }
        for type in HealthBackgroundType.allCases { wake(type, completion: HealthBackgroundCompletion({})) }
    }

    private func wake(_ type: HealthBackgroundType, completion: HealthBackgroundCompletion) {
        guard let context, context.permitted else { completion.finish(); return }
        completions[type, default: []].append(completion)
        dirty.insert(type)
        guard flights[type] == nil else { return }
        let flight = UUID(), token = generation
        flights[type] = flight
        tasks[type] = Task { [weak self] in
            guard let self else { return }
            do {
                repeat {
                    self.dirty.remove(type)
                    try await self.drain(type, scope: context.scope, token: token, flight: flight)
                } while self.dirty.contains(type)
            } catch {
                if self.valid(type, token: token, flight: flight) {
                    let value: Status = error as? HealthBackgroundFailure == .incrementalTransportUnavailable ? .transportUnavailable :
                        (error as? HealthBackgroundFailure == .protectedData ? .waitingForProtectedData : .retryRequired)
                    self.status = value; self.typeStatus[type] = value
                }
            }
            self.finish(type, flight: flight)
        }
        deadlines[type] = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.valid(type, token: token, flight: flight) else { return }
            self.status = .retryRequired; self.typeStatus[type] = .retryRequired
            self.tasks[type]?.cancel(); self.source.cancel(type)
            self.finish(type, flight: flight)
        }
    }
    private func valid(_ type: HealthBackgroundType, token: UUID, flight: UUID) -> Bool {
        generation == token && flights[type] == flight && context?.permitted == true
    }
    private func check(_ type: HealthBackgroundType, token: UUID, flight: UUID) throws {
        try Task.checkCancellation()
        guard valid(type, token: token, flight: flight) else { throw HealthBackgroundFailure.staleContext }
        guard protectedData() else { throw HealthBackgroundFailure.protectedData }
    }
    private func drain(_ type: HealthBackgroundType, scope: HealthBackgroundScope, token: UUID, flight: UUID) async throws {
        try check(type, token: token, flight: flight)
        var cursor: HealthBackgroundCursor
        if let saved = try checkpoints.load(scope: scope, type: type) { cursor = saved }
        else {
            var calendar = Calendar(identifier: .gregorian); calendar.timeZone = TimeZone(secondsFromGMT: 0)!
            cursor = HealthBackgroundCursor(start: calendar.date(byAdding: .month, value: -6, to: calendar.startOfDay(for: now()))!, anchor: nil)
            try checkpoints.save(cursor, scope: scope, type: type)
        }
        while true {
            try check(type, token: token, flight: flight)
            let page = try await source.page(type: type, cursor: cursor)
            try check(type, token: token, flight: flight)
            guard page.changeCount >= 0, !page.nextAnchor.isEmpty else { throw HealthBackgroundFailure.invalidPage }
            // Empty results never establish read access or move the query anchor. They may
            // discharge ONLY an obligation created by a previously durably admitted page.
            guard page.changeCount > 0 else {
                guard !page.hasMore else { throw HealthBackgroundFailure.invalidPage }
                if cursor.terminalPending {
                    guard let anchor = cursor.anchor else { throw HealthBackgroundFailure.invalidCheckpoint }
                    // Use the admitted anchor as stable terminal identity, not the possibly
                    // different anchor returned by this empty query. Failed checkpoint writes
                    // must replay this SAME terminal admission through the typed outbox.
                    let terminal = HealthBackgroundPage(changes: page.changes, nextAnchor: anchor, changeCount: 0, hasMore: false)
                    let receipt = try await admit(scope, type, terminal)
                    try check(type, token: token, flight: flight)
                    guard receipt == HealthBackgroundAdmission(scope: scope, type: type, anchor: anchor) else {
                        throw HealthBackgroundFailure.staleContext
                    }
                    cursor = HealthBackgroundCursor(start: cursor.start, anchor: anchor, terminalPending: false)
                    try checkpoints.save(cursor, scope: scope, type: type)
                }
                typeStatus[type] = .noReadableChanges; status = .noReadableChanges; return
            }
            guard page.nextAnchor != cursor.anchor else { throw HealthBackgroundFailure.invalidPage }
            let receipt = try await admit(scope, type, page)
            try check(type, token: token, flight: flight)
            guard receipt == HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor) else {
                throw HealthBackgroundFailure.staleContext
            }
            cursor = HealthBackgroundCursor(start: cursor.start, anchor: page.nextAnchor, terminalPending: page.hasMore)
            try checkpoints.save(cursor, scope: scope, type: type)
            typeStatus[type] = .admitted; status = .admitted
            if !page.hasMore { return }
        }
    }
    private func finish(_ type: HealthBackgroundType, flight: UUID) {
        guard flights[type] == flight else { return }
        flights.removeValue(forKey: type); tasks.removeValue(forKey: type)
        deadlines.removeValue(forKey: type)?.cancel(); dirty.remove(type)
        let pending = completions.removeValue(forKey: type) ?? []
        pending.forEach { $0.finish() }
    }
}

#if os(iOS)
import HealthKit
import UIKit

/// Raw objects stay in memory until the owner's typed ingestion normalization/admission.
/// HKDeletedObject only supplies a UUID: the ingestion contract needs a durable UUID ->
/// normalized identity mapping, including step-day dependencies, before this can be enabled.
struct HealthBackgroundChanges {
    let samples: [HKSample]
    let deleted: [HKDeletedObject]
}

@MainActor final class HealthKitBackgroundSource: HealthBackgroundSource {
    typealias Changes = HealthBackgroundChanges
    private let store: HKHealthStore
    private var observers: [HKObserverQuery] = []
    private var queries: [HealthBackgroundType: HKAnchoredObjectQuery] = [:]
    private var pending: [HealthBackgroundType: CheckedContinuation<HealthBackgroundPage<Changes>, Error>] = [:]
    private var queryIDs: [HealthBackgroundType: UUID] = [:]
    private var generation = UUID()
    // OS delivery registration is process-wide, so serialize across account/source instances.
    private static var deliveryTask: Task<Void, Never>?
    /// Stopping local observers always succeeds; OS delivery deregistration can fail.
    private(set) var backgroundDeliveryDisableFailed = false
    private let limit = 100
    init(store: HKHealthStore = HKHealthStore()) { self.store = store }
    private func sampleType(_ type: HealthBackgroundType) -> HKSampleType {
        switch type {
        case .stepCount: return HKQuantityType(.stepCount)
        case .sleepAnalysis: return HKCategoryType(.sleepAnalysis)
        case .heartRate: return HKQuantityType(.heartRate)
        case .restingHeartRate: return HKQuantityType(.restingHeartRate)
        case .workout: return HKObjectType.workoutType()
        }
    }
    func start(_ update: @escaping @MainActor (HealthBackgroundType, HealthBackgroundCompletion, Bool) -> Void) {
        stop()
        guard HKHealthStore.isHealthDataAvailable() else {
            for type in HealthBackgroundType.allCases { update(type, HealthBackgroundCompletion({}), true) }
            return
        }
        let token = generation
        for type in HealthBackgroundType.allCases {
            let query = HKObserverQuery(sampleType: sampleType(type), predicate: nil) { [weak self] _, done, error in
                let completion = HealthBackgroundCompletion(done)
                let failed = error != nil
                Task { @MainActor [weak self] in
                    guard let self, self.generation == token else { completion.finish(); return }
                    update(type, completion, failed)
                }
            }
            observers.append(query); store.execute(query)
        }
        // Serialize disable/enable operations. An old logout's disable must not race a new login.
        let previous = Self.deliveryTask
        Self.deliveryTask = Task { [weak self] in
            await previous?.value
            guard let self, self.generation == token else { return }
            for type in HealthBackgroundType.allCases {
                guard self.generation == token else { return }
                do { try await self.store.enableBackgroundDelivery(for: self.sampleType(type), frequency: .hourly) }
                catch { if self.generation == token { update(type, HealthBackgroundCompletion({}), true) } }
            }
        }
    }
    func stop() {
        generation = UUID()
        observers.forEach { store.stop($0) }; observers.removeAll()
        for type in HealthBackgroundType.allCases { cancel(type) }
        guard HKHealthStore.isHealthDataAvailable() else { return }
        let previous = Self.deliveryTask, store = store
        let types = HealthBackgroundType.allCases.map { sampleType($0) }
        Self.deliveryTask = Task { [weak self] in
            await previous?.value
            var failed = false
            for type in types {
                do { try await store.disableBackgroundDelivery(for: type) }
                catch { failed = true }
            }
            self?.backgroundDeliveryDisableFailed = failed
            // Failure cannot be repaired while locked; callbacks remain fenced and next start
            // retries disable before enable. Do not disable other features' HealthKit types.
        }
    }
    func cancel(_ type: HealthBackgroundType) {
        if let query = queries.removeValue(forKey: type) { store.stop(query) }
        queryIDs.removeValue(forKey: type)
        pending.removeValue(forKey: type)?.resume(throwing: CancellationError())
    }
    func page(type: HealthBackgroundType, cursor: HealthBackgroundCursor) async throws -> HealthBackgroundPage<Changes> {
        guard HKHealthStore.isHealthDataAvailable() else { throw HealthBackgroundFailure.unavailable }
        guard UIApplication.shared.isProtectedDataAvailable else { throw HealthBackgroundFailure.protectedData }
        guard pending[type] == nil else { throw HealthBackgroundFailure.queryFailed }
        let anchor: HKQueryAnchor?
        if let data = cursor.anchor {
            guard let decoded = try NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data) else {
                throw HealthBackgroundFailure.invalidCheckpoint
            }
            anchor = decoded
        } else { anchor = nil }
        let id = UUID()
        return try await withCheckedThrowingContinuation { continuation in
            pending[type] = continuation; queryIDs[type] = id
            let predicate = HKQuery.predicateForSamples(withStart: cursor.start, end: nil)
            let query = HKAnchoredObjectQuery(type: sampleType(type), predicate: predicate, anchor: anchor, limit: limit) { [weak self] _, samples, deleted, next, error in
                Task { @MainActor [weak self] in
                    guard let self, self.queryIDs[type] == id, let continuation = self.pending.removeValue(forKey: type) else { return }
                    self.queries.removeValue(forKey: type); self.queryIDs.removeValue(forKey: type)
                    do {
                        guard error == nil, let samples, let deleted, let next else { throw HealthBackgroundFailure.queryFailed }
                        let encoded = try NSKeyedArchiver.archivedData(withRootObject: next, requiringSecureCoding: true)
                        let count = samples.count + deleted.count
                        continuation.resume(returning: HealthBackgroundPage(changes: Changes(samples: samples, deleted: deleted), nextAnchor: encoded, changeCount: count, hasMore: count >= self.limit))
                    } catch { continuation.resume(throwing: HealthBackgroundFailure.queryFailed) }
                }
            }
            queries[type] = query; store.execute(query)
        }
    }
}
#endif
