# Local Companion — native iOS 17+

Native SwiftUI Today / Ledger / Settings app with a dependency-free Foundation Swift package. Frontend source is under `fe/`; contracts and backend implementation remain separate and read-only to this frontend work.

## Current verification

**The complete `RuntimeSmokeTests.testSyntheticUserJourney` passed: 1 actual XCTest UI test, 0 failures, 0 skipped**, on iOS 18.3.1. This is the full journey, not the shorter continuation. It passed on the first attempt of the final verification pass; no fix/rerun cycle was needed. Result: `fe/evidence/ui/FullJourney-01.xcresult`; summary: `fe/evidence/ui/full-runtime-summary.json`. The seeded app was left running for preview. Runtime identifiers and process IDs stay in ignored evidence, not source.

- Full Xcode app build, simulator installation and actual app launch succeed. The original CoreDevice/Mercury loader failure was resolved by the owner restoring the original Xcode resources and completing setup; it is **not a current build blocker**. No agent accepted licenses or changed system frameworks.
- Actual iOS URLSession requests reach the isolated loopback backend. The UI tests exercise explicit synthetic seed consent, ledger detail, correctness feedback, preferences, relaunch persistence, scripted processing, suppression, deletion/epoch rotation and explicit reseeding.
- The exact latest UI test result, screenshots and test destination are recorded under ignored `fe/evidence/ui`. Earlier failed attempts remain available; compile success and partial journeys are not counted as full UI passes.
- **32 real Swift Testing core tests pass on macOS.** These are separate from the iOS XCTest UI journey. Contract checks validated 14 Swift-encoded payload cases; ASGI interoperability and Swift decoding provide additional non-UI coverage.
- Complete device/simulator direct typecheck, object compilation and linking also passed. Direct-link ZIP artifacts are historical, unvalidated bundles; use the Xcode project for the installed app.

## Clean-checkout prerequisites

A complete Xcode installation with owner-completed setup and an installed compatible simulator runtime is required. iOS 18.3.1 was used for runtime verification; the deployment target remains iOS 17. Python 3 is needed for tooling only. No network Swift packages or XcodeGen are required: the project and generated Codable models are committed.

Start the backend using its documented setup and a **separate empty synthetic test database** at `http://127.0.0.1:8000`. For destructive UI tests, reserve a dedicated simulator with no personal app cache. Never point these tests at real data. The frontend scripts neither install runtimes nor accept licenses.

The UI runner chooses Xcode from explicit `DEVELOPER_DIR`, then a selected full Xcode installation, then the standard Xcode application location. An explicit invalid selection fails rather than silently switching. Build/core scripts also honor `DEVELOPER_DIR`. Set it to an Xcode `.app` or its `Contents/Developer` directory when using a nonstandard installation. No global `xcode-select` change is required.

## Build and core tests

From the workspace root:

```sh
./fe/scripts/test-core.sh
./fe/scripts/typecheck-ios.sh
python3 fe/scripts/toolchain.py xcode
```

The scripts create their output/cache directories under `fe/evidence`, use explicit SDK paths and strip inherited `DYLD_*`. Core tests supply only the installed platform testing-framework paths; the obsolete Mercury override has been removed. Python can be selected with `PYTHON` for the shell launchers. These build commands do not boot simulators, modify system libraries, stage, commit or push files.

## Full simulator UI journey

Inspect the authorized isolated backend before resetting it: every observation must be `mode=fixture`, `source_id=demo`; all non-demo sources must be disabled and not consented, with no private notes or cloud consent. Only then use the contract's confirmed `DELETE /data` request with the current epoch to obtain a pristine test instance. The UI runner intentionally **does not reset any backend automatically**.

The runner checks the backend is empty and has no non-demo source consent, notes or cloud consent before launching XCTest. The tests repeat these checks **before launching the app or taking their first screenshot**. They never request HealthKit or notification permissions. XCTest artifacts and screenshots may still contain test data, so all result bundles remain ignored.

Provide the dedicated simulator explicitly; no ignored device-ID file is required on a clean checkout:

```sh
export DEVICE_ID="YOUR_DEDICATED_SIMULATOR_UDID"
python3 fe/scripts/run-ui-smoke.py FullJourney --device "$DEVICE_ID"

# Equivalent: DEVICE_ID supplies the destination without --device.
python3 fe/scripts/run-ui-smoke.py AnotherFullJourney
```

Use a unique result name each time; previous failures are never overwritten. The default selected test is **RuntimeSmokeTests.testSyntheticUserJourney**, not the shorter continuation. The runner derives `ARCHS` from the host (arm64 or x86_64), uses its own DerivedData and disables parallel UI testing. The optional second positional argument preserves targeted invocation:

```sh
python3 fe/scripts/run-ui-smoke.py IndependentSuppression testSuppressionDeletionAndPreview --device "$DEVICE_ID"
```

Both tests require pristine input and seed their own fixtures; the shorter test no longer assumes preferences or synthesis left by another run. Reset the authorized synthetic database again before another destructive test. A legacy `fe/evidence/ui/device-id.txt` is accepted only as a last-resort fallback for existing local workflows. Explicit `--device` takes priority over `DEVICE_ID`, which takes priority over that file.

The full journey waits on committed revisions, process completion and epoch changes rather than assuming disappearance of a spinner means a network mutation has finished. It leaves explicitly seeded fixtures after verifying deletion and relaunch. To preview afterwards, launch `org.localcompanion.prototype` on the same dedicated simulator. The normal app never seeds automatically.

## Manual app flow

1. Open `fe/LocalCompanion.xcodeproj`, select **LocalCompanion**, and run on a simulator connected to the local backend.
2. Refresh, then explicitly consent to **Load representative synthetic demo**. The demo shows recorded values and fixture labels, not personal history or clinical conclusions.
3. Inspect ledger evidence revisions, coverage, uncertainty and limitations. Correctness and usefulness are separate feedback types; suppression persists until explicitly cleared.
4. Settings → **Refresh and edit preferences** saves a complete replacement. Stale forms are rejected; pending mutations retain their original request identities.
5. Optional HealthKit uses a separate explicit app-sync consent and Apple read-permission action. No live collection occurs at launch. Optional generic reminders likewise require a separate preference and OS authorization.
6. Delete all data only after confirmation. Offline deletion remains pending rather than being described as complete; deletion never modifies Apple's Health store.

The endpoint is fixed to `http://127.0.0.1:8000`, with no authentication or provider-key UI. Other hosts/ports and redirects are rejected. Actual simulator loopback access is verified. On a physical phone, loopback refers to the phone, not the Mac; physical-device backend networking needs a separately approved boundary.

## Contract and retry behavior

- Generated Codable types use exact snake_case keys, no implicit key conversion. Decoding rejects missing/unknown properties and invalid enum/version values. Nullable required keys are explicitly encoded as JSON null, not omitted. Dates stay strings.
- The schema is the source of truth. Generation is optional; committed generated models/project are immediately usable:

```sh
python3 fe/scripts/generate-models.py
python3 fe/scripts/generate-project.py
```

- Every mutation is encoded once and durably saved before sending. Retries send the same bytes, request ID and epoch, at most three attempts per sync with bounded backoff. Only transport failures or retryable server errors retry. Permanent rejection persists a blocked marker; relaunch/foreground refresh does not blindly resend it. Explicit discard is required before a replacement edit.
- State is fetched before draining the queue. A different server epoch discards old pending mutations; **nothing retags them**. A same-epoch lower revision, including an idempotency receipt, cannot roll the cached projection backward. A fresh GET follows successful mutations.
- Snapshot/outbox are atomically persisted together. Storage failure/corruption is not treated as an empty queue. Requests older than the 90-day receipt window are discarded with an explicit warning instead of resent.
- Mutations/HealthKit actions are serialized. Preferences require a fresh snapshot and unchanged editor baseline. Pending requests pause suggestion display/reminder scheduling conservatively until reconciled.
- Ledger facts, recurrence thresholds, coverage conclusions, suggestion filtering, cooldowns, synthesis and scheduling decisions remain on the backend. The client only normalizes daily counts, renders returned state, records explicit feedback and reconciles intent identifiers.

## HealthKit boundary and limits

Read-only step type, foreground only, no workouts/location/microphone/Motion permission, no background entitlement, and no HealthKit writes. The adapter requests cumulative statistics for each of seven completed dates in the preference IANA timezone. It uses calendar day boundaries rather than fixed 24-hour increments. It does not separately sum phone/watch samples. Changed aggregates reuse observation identity with the next revision and an exact supersedes reference; timezone changes do not rewrite old records.

Authorization-request completion is **not** read permission. Empty queries and errors report read_access=unknown; only actual returned quantities produce samples_readable. Empty is never converted to zero. Explicit returned zero remains a recorded value, not evidence of inactivity. Queries cannot establish continuous wear/carry coverage; successful observations are partial at most. Protected-data/unavailable/query-failed outcomes contain no raw error or health payload.

Device comparison against Health UI, multi-source reconciliation, limited authorization history, revocation and locked-device behavior remain unverified. Only seven completed days are refreshed; this is not a 90-day rescan or anchored deletion tracker. An empty query cannot distinguish revocation, deletion or genuine absence, so previously uploaded evidence is **not** tombstoned on empty results. Late changes outside the refresh window are not detected by this adapter. Disconnecting immediately excludes the source on the backend but preserves historical evidence until retention or explicit deletion.

## Notifications and privacy

- Generic fixed title/body; no record details in notification payloads. OS permission is checked separately from the backend preference. Notifications are upserted by epoch + backend intent ID and absent/expired intents are removed. Suppression, revocation, pending mutations, offline uncertainty and reset/delete cancel stale reminders.
- iOS can defer/suppress delivery. There is no delivery acknowledgment and no exact daily wakeup promise. Expiry is reconciled on foreground refresh; exact cancellation at midnight while terminated is not guaranteed.
- Live normalized data and the outbox are kept in Application Support with complete file protection on iOS and backup exclusion on both directory and snapshot. No iCloud/CloudKit entitlement or ubiquitous store, no analytics, and no persisted URL cache/cookies.
- Backup exclusion is tested on macOS; iOS locked-device enforcement and backup behavior remain device verification gates. Atomic replacement is not a secure-disk-overwrite guarantee.
- No provider keys/UI. Plaid and email stay visibly not_configured. Cloud consent defaults off; optional live health transmission is not enabled by v1. Scripted synthesis is explicitly labeled and disabled in the UI when live evidence exists.

## Additional reproducible checks

On a clean checkout, **run the core tests first**: they create `fe/evidence/encoded-contracts.json` and the required evidence/cache directories. Validation depends on that generated file. ASGI interoperability then creates `backend-state-responses.json`, which the Swift decoder check consumes. The backend virtual environment must already be set up according to backend instructions, including jsonschema/FastAPI test dependencies; these frontend checks install nothing. ASGI tests use a temporary database under frontend evidence, no listening port, and synthetic records only:

```sh
./fe/scripts/test-core.sh
PYTHONDONTWRITEBYTECODE=1 be/.venv/bin/python fe/scripts/validate-encoded-contracts.py
PYTHONDONTWRITEBYTECODE=1 HOME="$PWD/fe/evidence/home" XDG_CACHE_HOME="$PWD/fe/evidence/cache" TMPDIR="$PWD/fe/evidence/tmp" \
  be/.venv/bin/python fe/scripts/check-backend-interop.py
python3 fe/scripts/toolchain.py decode-backend
plutil -lint fe/LocalCompanion.xcodeproj/project.pbxproj fe/App/Info.plist fe/App/LocalCompanion.entitlements
```

No backend service was launched or left running by these checks. The ASGI response snapshots are then decoded by the actual Swift Codable types. This is interoperability evidence, **not** proof of app-to-backend networking on iOS.

## Evidence inventory

All generated files/logs are under `fe/evidence`, including:

- `core-tests.json`: exact command/environment, exit status, and real Swift Testing output/count.
- `encoded-contracts.json`, `contract-validation.json`: actual Swift payloads and JSON Schema results.
- `backend-interop.json`, `backend-state-responses.json`, `backend-decoder-run.json`: synthetic real-backend interoperability and Swift decoding results.
- `device-{core,typecheck,objects,link,platform}.json` and corresponding `simulator-*`: exact compiler/linker results and platform inspection.
- `device/LocalCompanion-unvalidated.zip`, `simulator/LocalCompanion-unvalidated.zip`: separately linked unvalidated app bundles; not installed/launched.
- `xcodebuild.json`: full-project build result. Earlier loader/CLT failures are historical, not current launch blockers.
- `ui/*.xcresult`, `ui/*-command.json`, screenshots and runtime summaries: actual simulator journeys, with failed and successful attempts kept separately.
- `verification-summary.json`: concise final gate results.

Remaining acceptance work: consult the current runtime summary for tested simulator flows; broader accessibility, real-device signing/HealthKit entitlement, actual queries, protection/backup behavior and notification delivery still require explicit device validation. Simulator runtime evidence is not clinical validation.
