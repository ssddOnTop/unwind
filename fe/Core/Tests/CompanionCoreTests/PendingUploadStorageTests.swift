import Foundation
import Testing
@testable import CompanionCore

/// Models the existing backend contract: exact receipt lookup precedes epoch rejection.
@MainActor private final class PendingRecoveryHTTP: HTTPTransport {
    var state = try! fixture()
    var requests: [URLRequest] = []
    var deletionReceipts: [String: (Data, State)] = [:]
    var loseDeletionReplies = false
    var deletionCommits = 0
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        switch request.url?.path {
        case "/state": return HTTPResult(status: 200, body: try Wire.encode(state))
        case "/experience/v1":
            let file = Bundle.module.url(forResource: "experience.example", withExtension: "json", subdirectory: "Fixtures")!
            var value = try Wire.decode(ExperienceV1.self, Data(contentsOf: file))
            value.data_epoch = state.data_epoch; value.revision = state.revision
            return HTTPResult(status: 200, body: try Wire.encode(value))
        case "/onboarding/v1":
            let value: [String: Any] = ["schema_version": "1", "data_epoch": state.data_epoch, "revision": 0,
                "status": "complete", "current_step": "complete", "legacy": true,
                "steps": ["health", "bank", "notifications"].map { ["id": $0, "title": $0, "purpose": "Optional", "optional": true, "decision": "pending"] as [String: Any] },
                "initial_sync": ["status": "deferred", "available_records": NSNull()]]
            return HTTPResult(status: 200, body: try JSONSerialization.data(withJSONObject: value))
        case "/data":
            let bytes = try #require(request.httpBody)
            let body = try Wire.decode(DeleteRequest.self, bytes)
            if let (original, receipt) = deletionReceipts[body.request_id] {
                guard original == bytes else { throw ClientFailure.rejected("idempotency_conflict", false) }
                if loseDeletionReplies { throw URLError(.networkConnectionLost) }
                return HTTPResult(status: 200, body: try Wire.encode(receipt))
            }
            guard body.data_epoch == state.data_epoch else {
                return HTTPResult(status: 409, body: try Wire.encode(APIErrorEnvelope(error: APIErrorEnvelopeError(code: "epoch_mismatch", message: "Stale epoch", retryable: false, request_id: body.request_id, details: []))))
            }
            deletionCommits += 1; state.data_epoch = "after-deletion-\(deletionCommits)"; state.revision += 1
            state.observations = []; state.ledger = []
            deletionReceipts[body.request_id] = (bytes, state)
            if loseDeletionReplies { throw URLError(.networkConnectionLost) }
            return HTTPResult(status: 200, body: try Wire.encode(state))
        default: throw ClientFailure.invalidEndpoint
        }
    }
}

@Suite struct PendingUploadStorageTests {
    private func legacyData(_ snapshot: LocalSnapshot) throws -> Data {
        var object = try dictionary(snapshot)
        object.removeValue(forKey: "dataEpoch") // Exact pre-envelope schema.
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }
    private func writeLegacy(_ snapshot: LocalSnapshot, to storage: ProtectedStorage) throws -> Data {
        try FileManager.default.createDirectory(at: storage.directory, withIntermediateDirectories: true)
        let data = try legacyData(snapshot)
        try data.write(to: storage.directory.appendingPathComponent("snapshot.json"), options: .atomic)
        return data
    }
    private func account(_ subject: String = "A", issuer: String = "https://issuer.example", session: String = "session") -> AuthIdentity {
        AuthIdentity(issuer: issuer, subject: subject, sessionID: session)
    }

    @Test @MainActor func coldRestartReadsNoDownloadedStateAndAcknowledgementRemovesPendingBytes() async throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        var state = try fixture(); state.preferences.notes = "DOWNLOADED-STATE-ONLY-SENTINEL"
        let transport = StubHTTP(state: state)
        let original = try CompanionClient(storage: storage, transport: transport)
        try await original.refresh()
        let item = try PendingMutation(path: "/feedback", request: feedback(epoch: state.data_epoch))
        try original.enqueue(item)
        let file = storage.directory.appendingPathComponent("snapshot.json")
        let stored = try Data(contentsOf: file)
        let text = String(decoding: stored, as: UTF8.self)
        #expect(!text.contains("DOWNLOADED-STATE-ONLY-SENTINEL"))
        let object = try #require(JSONSerialization.jsonObject(with: stored) as? [String: Any])
        #expect(object["version"] as? Int == 1)
        #expect(object["state"] == nil && object["ledger"] == nil && object["observations"] == nil)
        #expect(text.contains(item.body.base64EncodedString()))
        let restarted = try CompanionClient(storage: storage, transport: transport)
        #expect(restarted.snapshot.state == nil)
        #expect(restarted.snapshot.dataEpoch == state.data_epoch)
        #expect(restarted.snapshot.outbox == [item])
        #expect(restarted.experienceSnapshot == nil && restarted.bankingSnapshot == nil)
        try await restarted.refresh()
        #expect(restarted.snapshot.state == state && restarted.snapshot.outbox == [item])
        try await restarted.synchronize()
        #expect(await transport.mutations().map(\.httpBody) == [item.body])
        #expect(try storage.load().outbox.isEmpty)
        #expect(try storage.load().state == nil)
        let acknowledged = String(decoding: try Data(contentsOf: file), as: UTF8.self)
        #expect(!acknowledged.contains(item.body.base64EncodedString()))
        #expect(!acknowledged.contains(item.id))
        #expect(!acknowledged.contains("DOWNLOADED-STATE-ONLY-SENTINEL"))
    }

    @Test @MainActor func legacyMigrationPreservesExactBytesAndAllHealthMetadataBeforeFreshFetch() async throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        var state = try fixture(); state.preferences.notes = "LEGACY-DOWNLOADED-MARKER"
        var item = try PendingMutation(path: "/feedback", request: feedback(epoch: state.data_epoch))
        item.blockedCode = "consent_required"
        var legacy = LocalSnapshot(state: state, outbox: [item])
        let checkpoint = HealthIncrementalCheckpoint(start: Date(timeIntervalSince1970: 1_700_000_000), anchor: Data([1, 2, 3]), terminalPending: true)
        legacy.healthStreams = ["heartRate": HealthIncrementalStream(epoch: state.data_epoch, streamID: UUID().uuidString, nextSequence: 2, checkpoint: checkpoint, lastAdmission: HealthIncrementalAdmission(anchor: Data([1, 2, 3]), hasMore: true, digest: "digest"))]
        legacy.healthAuthorizationEpoch = state.data_epoch; legacy.healthCollectionPaused = true
        _ = try writeLegacy(legacy, to: storage)
        let client = try CompanionClient(storage: storage, transport: StubHTTP(state: state))
        #expect(client.snapshot.state == nil)
        #expect(client.snapshot.outbox == [item])
        #expect(client.snapshot.outbox.first?.body == item.body)
        #expect(client.snapshot.healthStreams == legacy.healthStreams)
        #expect(client.snapshot.healthAuthorizationEpoch == legacy.healthAuthorizationEpoch)
        #expect(client.snapshot.healthCollectionPaused == true)
        let migrated = String(decoding: try Data(contentsOf: storage.directory.appendingPathComponent("snapshot.json")), as: UTF8.self)
        #expect(!migrated.contains("LEGACY-DOWNLOADED-MARKER"))
        try await client.refresh()
        #expect(client.snapshot.outbox == [item])
        #expect(client.snapshot.healthStreams == legacy.healthStreams)
        #expect(try storage.load().state == nil)
        #expect(try storage.load().healthStreams == legacy.healthStreams)
    }

    @Test @MainActor func remoteEpochChangeFencesInsteadOfErasingLegacyUploads() async throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let state = try fixture(), item = try PendingMutation(path: "/feedback", request: feedback())
        var legacy = LocalSnapshot(state: state, outbox: [item])
        legacy.healthStreams = ["heartRate": HealthIncrementalStream(epoch: state.data_epoch, streamID: UUID().uuidString, nextSequence: 2,
            checkpoint: HealthIncrementalCheckpoint(start: Date(timeIntervalSince1970: 1_700_000_000), anchor: Data([9]), terminalPending: true), lastAdmission: nil)]
        legacy.healthAuthorizationEpoch = state.data_epoch; legacy.healthCollectionPaused = true
        _ = try writeLegacy(legacy, to: storage)
        var remote = state; remote.data_epoch = "new-server-epoch"
        let transport = StubHTTP(state: remote)
        let client = try CompanionClient(storage: storage, transport: transport)
        try await client.synchronize()
        #expect(await transport.mutations().isEmpty)
        #expect(client.snapshot.state == remote && client.snapshot.dataEpoch == remote.data_epoch)
        #expect(client.snapshot.outbox.isEmpty)
        #expect(client.snapshot.quarantinedUploads?.first?.outbox == [item])
        #expect(client.snapshot.quarantinedUploads?.first?.healthStreams == legacy.healthStreams)
        #expect(client.snapshot.quarantinedUploads?.first?.healthAuthorizationEpoch == legacy.healthAuthorizationEpoch)
        #expect(client.snapshot.quarantinedUploads?.first?.healthCollectionPaused == true)
        #expect(client.snapshot.healthStreams == nil && client.snapshot.healthAuthorizationEpoch == nil)
        #expect(try storage.load().quarantinedUploads?.first?.outbox == [item])
        #expect(throws: ClientFailure.rejected("epoch_mismatch", false)) { try client.enqueue(item) }
        let restarted = try CompanionClient(storage: storage, transport: transport)
        try await restarted.synchronize()
        #expect(restarted.snapshot.state == remote && restarted.snapshot.quarantinedUploadCount == 1)
        #expect(await transport.mutations().isEmpty)
        try restarted.discardQuarantinedUploads()
        #expect(try storage.load().quarantinedUploadCount == 0)
    }

    @Test func unknownSchemaCorruptionAndUnreadableStorageFailClosed() throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        try storage.save(LocalSnapshot(state: fixture(), outbox: [PendingMutation(path: "/feedback", request: feedback())]))
        let file = storage.directory.appendingPathComponent("snapshot.json")
        let original = try Data(contentsOf: file)
        for bad in [Data("broken".utf8), Data("{\"version\":99,\"outbox\":[]}".utf8), Data("{\"version\":1,\"outbox\":[],\"state\":{}}".utf8)] {
            try bad.write(to: file)
            #expect(throws: (any Error).self) { try storage.load() }
            #expect(try Data(contentsOf: file) == bad)
        }
        try original.write(to: file)
        #expect(try storage.load().outbox.count == 1)
        try FileManager.default.removeItem(at: file)
        try FileManager.default.createDirectory(at: file, withIntermediateDirectories: false)
        #expect(throws: (any Error).self) { try storage.load() }
    }

    @Test func inconsistentLegacyEpochMigrationDoesNotRewriteOriginal() throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let legacy = LocalSnapshot(state: try fixture(), outbox: [try PendingMutation(path: "/feedback", request: feedback(epoch: "different"))])
        let original = try writeLegacy(legacy, to: storage)
        #expect(throws: (any Error).self) { try storage.load() }
        #expect(try Data(contentsOf: storage.directory.appendingPathComponent("snapshot.json")) == original)
    }

    @Test @MainActor func logoutSwitchDeploymentAndIssuerIsolationRetainOnlyTheirOwnQueues() async throws {
        let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
        let cache = AccountCache(root: root, deployment: "https://one.example")
        let storageA = try cache.activate(account())
        let itemA = try PendingMutation(path: "/feedback", request: feedback())
        try storageA.save(LocalSnapshot(state: fixture(), outbox: [itemA]))
        try cache.clearActive()
        let storageB = try cache.activate(account("B"))
        #expect(try storageB.load().outbox.isEmpty)
        let transportB = StubHTTP(state: try fixture())
        let clientB = try CompanionClient(storage: storageB, transport: transportB)
        try await clientB.synchronize()
        #expect(await transportB.mutations().isEmpty)
        #expect(try storageA.load().outbox == [itemA])
        let otherIssuer = try cache.activate(account(issuer: "https://different-issuer.example"))
        #expect(otherIssuer.directory != storageA.directory)
        #expect(try otherIssuer.load().outbox.isEmpty)
        let otherDeployment = AccountCache(root: root, deployment: "https://two.example")
        let isolated = try otherDeployment.activate(account())
        #expect(isolated.directory != storageA.directory)
        #expect(try isolated.load().outbox.isEmpty)
        try otherDeployment.clearActive()
        let resumed = try cache.activate(account(session: "new-session"))
        #expect(resumed.directory == storageA.directory)
        #expect(try resumed.load().outbox == [itemA])
        #expect(try resumed.load().state == nil)
    }

    @Test func legacyNamespaceMigrationUsesRecordedDeploymentEvenWhenSwitching() throws {
        let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
        let cacheA = AccountCache(root: root, deployment: "https://original.example")
        _ = try cacheA.activate(account()) // Establish the old deployment/account marker pair.
        let legacy = ProtectedStorage(directory: root.appendingPathComponent(account().accountNamespace))
        let item = try PendingMutation(path: "/feedback", request: feedback())
        _ = try writeLegacy(LocalSnapshot(state: fixture(), outbox: [item]), to: legacy)
        let cacheB = AccountCache(root: root, deployment: "https://replacement.example")
        let replacement = try cacheB.activate(account("B"))
        #expect(try replacement.load().outbox.isEmpty)
        #expect(!FileManager.default.fileExists(atPath: legacy.directory.path))
        let migrated = ProtectedStorage(directory: cacheA.directory(for: account()))
        #expect(try migrated.load().outbox == [item])
        #expect(try migrated.load().state == nil)
        #expect(try cacheB.activate(account()).load().outbox.isEmpty)
        #expect(try cacheA.activate(account()).load().outbox == [item])
    }

    @Test @MainActor func deletionCommitsAllRepliesLostThenRestartRecoversOriginalReceiptWithoutErasingAgain() async throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let http = PendingRecoveryHTTP()
        let client = try CompanionClient(storage: storage, transport: http, retryDelay: 0)
        try await client.refresh()
        let epoch = try #require(client.snapshot.dataEpoch)
        let deletion = try PendingMutation(path: "/data", method: "DELETE", request: DeleteRequest(schema_version: "1", request_id: "original-deletion", data_epoch: epoch, confirm: "delete_all"))
        try client.enqueue(deletion)
        // Another old pending obligation must be retired when this explicit deletion is acknowledged.
        try client.enqueue(PendingMutation(path: "/feedback", request: feedback(epoch: epoch)))
        http.loseDeletionReplies = true
        await #expect(throws: (any Error).self) { try await client.synchronize() }
        #expect(http.deletionCommits == 1 && client.snapshot.outbox.count == 2)
        #expect(http.requests.filter { $0.url?.path == "/data" }.count == 3)
        // New post-erasure data must not be deleted or replaced with the old receipt projection.
        http.state.preferences.notes = "NEW-CURRENT-DOWNLOADED-STATE"; http.state.revision += 10
        let restarted = try CompanionClient(storage: storage, transport: http, retryDelay: 0)
        try await restarted.synchronize() // Still loses the replay, but current account remains usable.
        #expect(restarted.snapshot.state == http.state && restarted.snapshot.quarantinedUploadCount == 2)
        #expect(!restarted.snapshot.hasGeneralPendingChanges && !restarted.snapshot.hidesCachedData)
        http.loseDeletionReplies = false
        try await restarted.recoverQuarantinedUploads()
        #expect(http.deletionCommits == 1)
        #expect(http.requests.filter { $0.url?.path == "/data" }.allSatisfy { $0.httpMethod == "DELETE" && $0.httpBody == deletion.body })
        #expect(!http.requests.contains { $0.url?.path == "/feedback" })
        #expect(restarted.snapshot.state == http.state)
        #expect(restarted.snapshot.quarantinedUploadCount == 0 && restarted.snapshot.outbox.isEmpty)
        let disk = String(decoding: try Data(contentsOf: storage.directory.appendingPathComponent("snapshot.json")), as: UTF8.self)
        #expect(!disk.contains(deletion.body.base64EncodedString()) && !disk.contains("NEW-CURRENT-DOWNLOADED-STATE"))
        let again = try CompanionClient(storage: storage, transport: http)
        try await again.synchronize()
        #expect(again.snapshot.quarantinedUploadCount == 0 && again.snapshot.state == http.state)
        #expect(http.deletionCommits == 1)
    }

    @Test @MainActor func unknownStaleDeletionCannotCreateNewEraseAndCanBeExplicitlyDiscarded() async throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let http = PendingRecoveryHTTP(), old = try fixture()
        let deletion = try PendingMutation(path: "/data", method: "DELETE", request: DeleteRequest(schema_version: "1", request_id: "never-committed-delete", data_epoch: old.data_epoch, confirm: "delete_all"))
        try storage.save(LocalSnapshot(state: old, outbox: [deletion]))
        http.state.data_epoch = "remote-reset"; http.state.revision += 1
        let client = try CompanionClient(storage: storage, transport: http)
        try await client.synchronize(); try await client.recoverQuarantinedUploads()
        #expect(http.deletionCommits == 0)
        #expect(client.snapshot.state == http.state && client.snapshot.quarantinedUploadCount == 1)
        #expect(http.requests.filter { $0.url?.path == "/data" }.allSatisfy { $0.httpBody == deletion.body })
        #expect(try storage.load().quarantinedUploads?.first?.outbox == [deletion])
        try client.discardQuarantinedUploads()
        #expect(try storage.load().quarantinedUploadCount == 0)
        #expect(client.snapshot.state == http.state && http.deletionCommits == 0)
    }

    @Test @MainActor func staleDeletionEnvelopeCannotSmuggleCurrentEpochEraseBody() async throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let http = PendingRecoveryHTTP(), old = try fixture()
        let deletion = try PendingMutation(path: "/data", method: "DELETE", request: DeleteRequest(schema_version: "1", request_id: "old-delete", data_epoch: old.data_epoch, confirm: "delete_all"))
        http.state.data_epoch = "new-current-epoch"
        var altered = try dictionary(deletion)
        altered["body"] = try Wire.encode(DeleteRequest(schema_version: "1", request_id: deletion.id, data_epoch: http.state.data_epoch, confirm: "delete_all")).base64EncodedString()
        let invalid = try Wire.decode(PendingMutation.self, JSONSerialization.data(withJSONObject: altered))
        try storage.save(LocalSnapshot(state: old, outbox: [invalid]))
        let client = try CompanionClient(storage: storage, transport: http)
        try await client.synchronize(); try await client.recoverQuarantinedUploads()
        #expect(http.deletionCommits == 0 && !http.requests.contains { $0.url?.path == "/data" })
        #expect(client.snapshot.state == http.state && client.snapshot.quarantinedUploadCount == 1)
        #expect(try storage.load().quarantinedUploads?.first?.outbox == [invalid])
    }

    @Test @MainActor func acknowledgedExplicitDeletionClearsQuarantineAndCannotRestoreIt() async throws {
        let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
        let http = PendingRecoveryHTTP(), old = try fixture()
        let item = try PendingMutation(path: "/feedback", request: feedback())
        try storage.save(LocalSnapshot(state: old, outbox: [item]))
        http.state.data_epoch = "new-current-epoch"
        let client = try CompanionClient(storage: storage, transport: http)
        try await client.synchronize(); #expect(client.snapshot.quarantinedUploadCount == 1)
        let deletion = try PendingMutation(path: "/data", method: "DELETE", request: DeleteRequest(schema_version: "1", request_id: "explicit-current-delete", data_epoch: http.state.data_epoch, confirm: "delete_all"))
        try client.enqueue(deletion); try await client.synchronize()
        #expect(client.snapshot.quarantinedUploadCount == 0 && client.snapshot.outbox.isEmpty)
        let restarted = try CompanionClient(storage: storage, transport: http)
        try await restarted.synchronize()
        #expect(restarted.snapshot.quarantinedUploadCount == 0 && restarted.snapshot.outbox.isEmpty)
        #expect(http.deletionCommits == 1 && http.requests.filter { $0.url?.path == "/feedback" }.isEmpty)
    }

    @Test @MainActor func quarantineAndExpiredUploadsLeaveOverviewOnboardingAndSettingsRecoveryReachable() async throws {
        for expired in [false, true] {
            let storage = temporaryStorage(); defer { try? FileManager.default.removeItem(at: storage.directory) }
            let http = PendingRecoveryHTTP(), old = try fixture()
            let item = try PendingMutation(path: "/feedback", request: feedback(), now: expired ? Date(timeIntervalSince1970: 0) : Date())
            try storage.save(LocalSnapshot(state: old, outbox: [item]))
            if !expired { http.state.data_epoch = "remotely-erased" }
            let client = try CompanionClient(storage: storage, transport: http)
            let model = AppModel(client: client, accountNamespace: "isolated-test")
            await model.refresh()
            #expect(model.connected && model.experience != nil && model.state == http.state)
            #expect(!model.hasPending && model.quarantinedUploadCount == 1)
            await model.onboarding.load()
            #expect(model.onboarding.canShowMain)
            #expect(!http.requests.contains(where: { $0.url?.path == "/feedback" }))
            await model.recoverQuarantinedUploads()
            #expect(model.connected && model.quarantinedUploadCount == 1)
            await model.discardQuarantinedUploads()
            #expect(model.connected && model.quarantinedUploadCount == 0)
            #expect(try storage.load().quarantinedUploadCount == 0)
            model.invalidateForSignOut()
        }
        let frontend = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let settings = try String(contentsOf: frontend.appendingPathComponent("App/SettingsView.swift"), encoding: .utf8)
        #expect(settings.contains("settings.quarantinedUploadCount"))
        #expect(settings.contains("settings.recoverQuarantinedUploads"))
        #expect(settings.contains("settings.discardQuarantinedUploads"))
        #expect(settings.contains("model.discardQuarantinedUploads()"))
    }

    @Test func malformedUnboundLegacyCannotBeRewrittenDuringActivation() throws {
        let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
        let cache = AccountCache(root: root, deployment: "https://original.example")
        let old = root.appendingPathComponent(account().accountNamespace)
        try FileManager.default.createDirectory(at: old, withIntermediateDirectories: true)
        let file = old.appendingPathComponent("snapshot.json"), original = Data("corrupt-snapshot".utf8)
        try original.write(to: file)
        #expect(throws: (any Error).self) { try cache.activate(account()) }
        #expect(try Data(contentsOf: file) == original)
        try FileManager.default.removeItem(at: file)
        try FileManager.default.createDirectory(at: file, withIntermediateDirectories: false)
        #expect(throws: (any Error).self) { try cache.activate(account()) }
        var isDirectory: ObjCBool = false
        #expect(FileManager.default.fileExists(atPath: file.path, isDirectory: &isDirectory) && isDirectory.boolValue)
    }

    @Test func legacyNamespaceWithoutBindingOrWithCollisionIsNotDeletedOrAdopted() throws {
        let root = temporaryStorage().directory; defer { try? FileManager.default.removeItem(at: root) }
        let cache = AccountCache(root: root, deployment: "https://original.example")
        let legacy = ProtectedStorage(directory: root.appendingPathComponent(account().accountNamespace))
        let item = try PendingMutation(path: "/feedback", request: feedback())
        var state = try fixture(); state.preferences.notes = "UNBOUND-DOWNLOADED-SENTINEL"
        _ = try writeLegacy(LocalSnapshot(state: state, outbox: [item]), to: legacy)
        #expect(throws: (any Error).self) { try cache.activate(account()) }
        let sanitized = try Data(contentsOf: legacy.directory.appendingPathComponent("snapshot.json"))
        #expect(!String(decoding: sanitized, as: UTF8.self).contains("UNBOUND-DOWNLOADED-SENTINEL"))
        #expect(try legacy.load().outbox == [item] && legacy.load().state == nil)
        // Supply valid historical bindings, but an already populated destination must still fail closed.
        let deployment = cache.directory(for: account()).deletingLastPathComponent().lastPathComponent
        try Data(deployment.utf8).write(to: root.appendingPathComponent("active-deployment"))
        try Data(account().accountNamespace.utf8).write(to: root.appendingPathComponent("active-account"))
        let destination = ProtectedStorage(directory: cache.directory(for: account()))
        _ = try writeLegacy(LocalSnapshot(state: state, outbox: [item]), to: destination)
        #expect(throws: (any Error).self) { try cache.activate(account()) }
        #expect(try Data(contentsOf: legacy.directory.appendingPathComponent("snapshot.json")) == sanitized)
        let destinationBytes = try Data(contentsOf: destination.directory.appendingPathComponent("snapshot.json"))
        #expect(!String(decoding: destinationBytes, as: UTF8.self).contains("UNBOUND-DOWNLOADED-SENTINEL"))
        #expect(try destination.load().outbox == [item] && destination.load().state == nil)
    }
}
