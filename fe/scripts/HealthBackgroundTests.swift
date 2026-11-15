import Foundation

// Standalone, offline executable: compile this file with HealthBackgroundCoordinator.swift.
// No app, simulator, HealthKit store, network, provider, or database is opened.
@MainActor private final class MemoryCheckpoints: HealthBackgroundCheckpointing {
    var values: [String: HealthBackgroundCursor] = [:]
    var failLoad = false
    var failCommit = false
    func key(_ scope: HealthBackgroundScope, _ type: HealthBackgroundType) -> String { scope.accountID + ":" + scope.epoch + ":" + type.rawValue }
    func load(scope: HealthBackgroundScope, type: HealthBackgroundType) throws -> HealthBackgroundCursor? {
        if failLoad { throw HealthBackgroundFailure.invalidCheckpoint }
        return values[key(scope, type)]
    }
    func save(_ cursor: HealthBackgroundCursor, scope: HealthBackgroundScope, type: HealthBackgroundType) throws {
        if failCommit && cursor.anchor != nil { throw HealthBackgroundFailure.protectedData }
        values[key(scope, type)] = cursor
    }
    func anchor(_ scope: HealthBackgroundScope, _ type: HealthBackgroundType = .stepCount) -> Data? { values[key(scope, type)]?.anchor }
}

@MainActor private final class FakeSource: HealthBackgroundSource {
    typealias Changes = [String]
    var callback: (@MainActor (HealthBackgroundType, HealthBackgroundCompletion, Bool) -> Void)?
    var starts = 0
    var stops = 0
    var calls: [(HealthBackgroundType, HealthBackgroundCursor)] = []
    var pages: [HealthBackgroundPage<Changes>] = []
    var fail = false
    var suspended: CheckedContinuation<HealthBackgroundPage<Changes>, Error>?
    var suspend = false
    var suspendAfterPages = false
    var cancellations = 0
    func start(_ update: @escaping @MainActor (HealthBackgroundType, HealthBackgroundCompletion, Bool) -> Void) { starts += 1; callback = update }
    func stop() { stops += 1; callback = nil; cancel(.stepCount) }
    func cancel(_ type: HealthBackgroundType) {
        guard type == .stepCount else { return }
        cancellations += 1
        let value = suspended; suspended = nil; value?.resume(throwing: CancellationError())
    }
    func page(type: HealthBackgroundType, cursor: HealthBackgroundCursor) async throws -> HealthBackgroundPage<Changes> {
        calls.append((type, cursor))
        if type == .stepCount {
            if fail { throw HealthBackgroundFailure.queryFailed }
            if suspend || (suspendAfterPages && pages.isEmpty) { return try await withCheckedThrowingContinuation { suspended = $0 } }
            if !pages.isEmpty { return pages.removeFirst() }
        }
        return HealthBackgroundPage(changes: [], nextAnchor: Data([255]), changeCount: 0, hasMore: false)
    }
    func emit(_ completion: HealthBackgroundCompletion, failed: Bool = false) { callback?(.stepCount, completion, failed) }
}

@MainActor private final class ProtectedDataState { var available = false }

@main struct HealthBackgroundTests {
    static let scope = HealthBackgroundScope(accountID: "account-A", epoch: "epoch-1")
    static func context(_ scope: HealthBackgroundScope = scope, authenticated: Bool = true, enabled: Bool = true, consent: Bool = true, requested: Bool = true) -> HealthBackgroundContext {
        HealthBackgroundContext(scope: scope, authenticated: authenticated, sourceEnabled: enabled, consentGranted: consent, authorizationRequested: requested)
    }
    static func page(_ id: UInt8, changes: [String] = ["insert"], more: Bool = false) -> HealthBackgroundPage<[String]> {
        HealthBackgroundPage(changes: changes, nextAnchor: Data([id]), changeCount: changes.count, hasMore: more)
    }
    @MainActor static func settle() async { for _ in 0..<150 { await Task.yield() } }
    static func expect(_ value: @autoclosure () -> Bool, _ message: String) { precondition(value(), message) }

    @MainActor static func main() async throws {
        var passed = 0
        // Denied app consent/auth and never-requested OS authorization cannot install observers.
        for value in [context(authenticated: false), context(enabled: false), context(consent: false), context(requested: false)] {
            let source = FakeSource(), storage = MemoryCheckpoints()
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true })
            coordinator.updateContext(value); await settle()
            expect(source.starts == 0 && source.calls.isEmpty, "unpermitted observers started")
            coordinator.stop()
        }; passed += 1
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1)]
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true })
            coordinator.updateContext(context()); await settle()
            expect(storage.anchor(scope) == nil, "unsupported snapshot transport advanced anchor")
            expect(coordinator.typeStatus[.stepCount] == .transportUnavailable, "missing transport not reported")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1, more: true), page(2, changes: ["delete:uuid"])]
            var admitted: [[String]] = []
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                expect(storage.anchor(scope) == (admitted.isEmpty ? nil : Data([1])), "anchor committed before durable admission")
                admitted.append(page.changes)
                return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(admitted == [["insert"], ["delete:uuid"]], "deletion-only page lost")
            expect(storage.anchor(scope) == Data([2]), "paged checkpoint missing")
            let cursors = source.calls.filter { $0.0 == .stepCount }.map { $0.1 }
            expect(cursors.count == 2 && cursors[1].anchor == Data([1]) && cursors[0].start == cursors[1].start, "unstable query boundary")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            var admits = 0
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                admits += 1; return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(admits == 0 && storage.values.values.allSatisfy { $0.anchor == nil }, "empty/denied result committed")
            expect(coordinator.typeStatus.values.allSatisfy { $0 == .noReadableChanges }, "empty read claimed permission")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            let protection = ProtectedDataState()
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { protection.available })
            coordinator.updateContext(context()); await settle()
            expect(source.calls.isEmpty && storage.values.isEmpty, "read while locked")
            protection.available = true; coordinator.retry(); await settle()
            expect(source.calls.count == 5, "unlock retry did not read five supported types")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            storage.failLoad = true
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true })
            coordinator.updateContext(context()); await settle()
            expect(source.calls.isEmpty && storage.values.isEmpty, "corrupt storage treated as fresh cursor")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1), page(1)]; storage.failCommit = true
            var receipts = 0
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                receipts += 1; return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(storage.anchor(scope) == nil && receipts == 1, "failed save advanced anchor")
            storage.failCommit = false; coordinator.retry(); await settle()
            expect(receipts == 2 && storage.anchor(scope) == Data([1]), "admitted page not replayed after save failure")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1)]
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { _, type, page in
                HealthBackgroundAdmission(scope: HealthBackgroundScope(accountID: "wrong", epoch: "epoch-1"), type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(storage.anchor(scope) == nil, "wrong account receipt accepted")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1)]
            var resume: CheckedContinuation<Void, Never>?
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                await withCheckedContinuation { resume = $0 }
                return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(resume != nil, "admission did not suspend")
            coordinator.stop(); resume?.resume(); await settle()
            expect(storage.anchor(scope) == nil && coordinator.status == .stopped, "logout allowed late receipt")
            passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.suspend = true
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true })
            coordinator.updateContext(context()); await settle()
            var count = 0
            let completion = HealthBackgroundCompletion { count += 1 }
            source.emit(completion); source.emit(completion)
            coordinator.stop(); await settle(); completion.finish()
            expect(count == 1 && storage.anchor(scope) == nil, "stop did not complete exactly once")
            passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.fail = true
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true })
            coordinator.updateContext(context()); await settle()
            var count = 0
            source.emit(HealthBackgroundCompletion { count += 1 }); await settle()
            expect(count == 1 && storage.anchor(scope) == nil, "query failure lost completion or advanced anchor")
            source.emit(HealthBackgroundCompletion { count += 1 }, failed: true)
            expect(count == 2, "observer error completion missing")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.suspend = true
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, timeoutNanoseconds: 20_000_000)
            coordinator.updateContext(context()); await settle()
            var count = 0
            source.emit(HealthBackgroundCompletion { count += 1 })
            try await Task.sleep(nanoseconds: 60_000_000); await settle()
            expect(count == 1 && storage.anchor(scope) == nil && source.suspended == nil, "timeout failed to release observer/query")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1)]
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            let next = HealthBackgroundScope(accountID: scope.accountID, epoch: "epoch-2")
            coordinator.updateContext(context(next)); await settle()
            expect(storage.anchor(scope) == Data([1]) && storage.anchor(next) == nil, "epoch anchor reused")
            expect(source.calls.last(where: { $0.0 == .stepCount })?.1.anchor == nil, "new epoch queried old anchor")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.suspend = true
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            var count = 0
            source.emit(HealthBackgroundCompletion { count += 1 })
            source.emit(HealthBackgroundCompletion { count += 1 })
            let continuation = source.suspended; source.suspended = nil; source.suspend = false
            continuation?.resume(returning: page(1)); await settle()
            expect(count == 2, "coalesced callbacks not completed")
            expect(source.calls.filter { $0.0 == .stepCount }.count == 2, "wake during flight not reconciled")
            expect(storage.anchor(scope) == Data([1]), "coalesced wake lost admission")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1, more: true), page(1)]
            var admissions = 0
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                admissions += 1
                return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(admissions == 1 && coordinator.typeStatus[.stepCount] == .retryRequired, "nonadvancing page loop")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1)]
            var admissions = 0
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                admissions += 1
                throw HealthBackgroundFailure.queryFailed
            })
            coordinator.updateContext(context()); await settle()
            expect(admissions == 1 && storage.anchor(scope) == nil, "failed outbox admission advanced cursor")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true })
            coordinator.updateContext(context()); await settle()
            let stale = source.callback
            coordinator.updateContext(context(HealthBackgroundScope(accountID: "new-account", epoch: scope.epoch))); await settle()
            let before = source.calls.count
            var completions = 0
            stale?(.stepCount, HealthBackgroundCompletion { completions += 1 }, false); await settle()
            expect(completions == 1 && source.calls.count == before, "stale observer crossed account boundary")
            coordinator.stop(); passed += 1
        }
        do {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("health-checkpoint-test-" + UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: directory) }
            let store = HealthBackgroundFileCheckpoints(directory: directory)
            let missing = try store.load(scope: scope, type: .stepCount)
            expect(missing == nil, "missing checkpoint not nil")
            let cursor = HealthBackgroundCursor(start: Date(timeIntervalSince1970: 1234), anchor: Data([1, 2]))
            try store.save(cursor, scope: scope, type: .stepCount)
            let reopened = HealthBackgroundFileCheckpoints(directory: directory)
            let restored = try reopened.load(scope: scope, type: .stepCount)
            let other = try reopened.load(scope: HealthBackgroundScope(accountID: "B", epoch: scope.epoch), type: .stepCount)
            expect(restored == cursor, "checkpoint not durable")
            expect(other == nil, "cross-account checkpoint")
            let file = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil).first!
            try Data("corrupt".utf8).write(to: file)
            do { _ = try reopened.load(scope: scope, type: .stepCount); preconditionFailure("corruption ignored") } catch {}
            passed += 1
        }
        for fullPages in [1, 2] {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = (1...fullPages).map { page(UInt8($0), changes: Array(repeating: "insert", count: 100), more: true) }
            var counts: [Int] = []
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                counts.append(page.changeCount)
                if page.changeCount == 0 {
                    expect(!page.hasMore && page.nextAnchor == Data([UInt8(fullPages)]), "terminal identity must be the last admitted anchor")
                    expect(storage.values[storage.key(scope, type)]?.terminalPending == true, "terminal admission lacked durable obligation")
                }
                return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(counts == Array(repeating: 100, count: fullPages) + [0], "exact page multiple omitted terminal empty")
            expect(storage.anchor(scope) == Data([UInt8(fullPages)]), "empty response advanced anchor")
            expect(storage.values[storage.key(scope, .stepCount)]?.terminalPending == false, "terminal obligation not cleared")
            coordinator.retry(); await settle()
            expect(counts.count == fullPages + 1, "already completed terminal was sent again")
            expect(coordinator.typeStatus[.stepCount] == .noReadableChanges, "empty terminal claimed readable permission")
            coordinator.stop(); passed += 1
        }
        do {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("health-terminal-relaunch-" + UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: directory) }
            let source = FakeSource(), storage = HealthBackgroundFileCheckpoints(directory: directory)
            source.pages = [page(1, changes: Array(repeating: "insert", count: 100), more: true)]
            source.suspendAfterPages = true
            let first = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            first.updateContext(context()); await settle()
            let pending = try storage.load(scope: scope, type: .stepCount)
            expect(pending?.terminalPending == true && source.suspended != nil, "full-page obligation was not durably checkpointed")
            first.stop(); await settle()
            let reopened = HealthBackgroundFileCheckpoints(directory: directory), restartedSource = FakeSource()
            var terminalCount = 0
            let second = HealthBackgroundCoordinator(source: restartedSource, checkpoints: reopened, protectedData: { true }, admit: { scope, type, page in
                expect(page.changeCount == 0 && !page.hasMore && page.nextAnchor == Data([1]), "relaunch did not resume exact terminal obligation")
                terminalCount += 1
                return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            second.updateContext(context()); await settle()
            let finished = try reopened.load(scope: scope, type: .stepCount)
            expect(terminalCount == 1 && finished?.terminalPending == false && finished?.anchor == Data([1]), "relaunch lost terminal publication or moved anchor")
            second.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1, changes: Array(repeating: "insert", count: 100), more: true)]
            var failTerminal = true, terminalCount = 0
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                if page.changeCount == 0 {
                    terminalCount += 1
                    if failTerminal { failTerminal = false; throw HealthBackgroundFailure.queryFailed }
                }
                return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(storage.values[storage.key(scope, .stepCount)]?.terminalPending == true, "failed terminal admission cleared obligation")
            coordinator.retry(); await settle()
            expect(terminalCount == 2 && storage.values[storage.key(scope, .stepCount)]?.terminalPending == false, "failed terminal admission did not retry")
            coordinator.stop(); passed += 1
        }
        do {
            let source = FakeSource(), storage = MemoryCheckpoints()
            source.pages = [page(1, changes: Array(repeating: "insert", count: 100), more: true)]
            var terminalAnchors: [Data] = []
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                if page.changeCount == 0 {
                    terminalAnchors.append(page.nextAnchor)
                    storage.failCommit = terminalAnchors.count == 1
                }
                return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(storage.values[storage.key(scope, .stepCount)]?.terminalPending == true, "failed terminal checkpoint lost replay obligation")
            coordinator.retry(); await settle()
            expect(terminalAnchors == [Data([1]),Data([1])], "terminal checkpoint retry changed identity")
            expect(storage.values[storage.key(scope, .stepCount)]?.terminalPending == false, "terminal checkpoint retry failed")
            coordinator.stop(); passed += 1
        }
        do {
            let old = try JSONDecoder().decode(HealthBackgroundCursor.self, from: Data("{\"start\":0,\"anchor\":\"AQ==\"}".utf8))
            expect(!old.terminalPending, "legacy anchor inferred a terminal obligation")
            let source = FakeSource(), storage = MemoryCheckpoints()
            try storage.save(old, scope: scope, type: .stepCount)
            var admits = 0
            let coordinator = HealthBackgroundCoordinator(source: source, checkpoints: storage, protectedData: { true }, admit: { scope, type, page in
                admits += 1; return HealthBackgroundAdmission(scope: scope, type: type, anchor: page.nextAnchor)
            })
            coordinator.updateContext(context()); await settle()
            expect(admits == 0 && storage.anchor(scope) == old.anchor, "empty denied/unreadable result fabricated terminal admission")
            coordinator.stop()
            do {
                _ = try JSONDecoder().decode(HealthBackgroundCursor.self, from: Data("{\"start\":0,\"terminalPending\":true}".utf8))
                preconditionFailure("terminal obligation without admitted anchor accepted")
            } catch {}
            passed += 1
        }
        print("PASS: \(passed) isolated Health background behavior checks")
    }
}
