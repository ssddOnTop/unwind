import SwiftUI
import CompanionCore

@main struct LocalCompanionApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            TabView {
                TodayView(model: model).tabItem { Label("Today", systemImage: "sun.max") }
                LedgerView(model: model).tabItem { Label("Ledger", systemImage: "list.bullet.rectangle") }
                SettingsView(model: model).tabItem { Label("Settings", systemImage: "gearshape") }
            }
            .safeAreaInset(edge: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    if model.busy { ProgressView("Syncing…") }
                    if model.hasPending { Text("\(model.snapshot.outbox.count) saved request(s) pending · suggestions and reminders paused").font(.caption) }
                    if let message = model.message { Text(message).font(.caption).foregroundStyle(.secondary) }
                }.padding(.horizontal).frame(maxWidth: .infinity, alignment: .leading).background(.bar)
            }
            .task { await model.refresh() }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { Task { await model.refresh() } }
            }
        }
    }
}

struct TodayView: View {
    @ObservedObject var model: AppModel
    @SwiftUI.State private var seedConfirmation = false
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("Your records, with context").font(.title2.bold())
                    Text("Optional suggestions, not diagnoses. Missing records are unknown—not inactivity or confirmation.").foregroundStyle(.secondary)
                }
                if let state = model.state {
                    Section("Snapshot") {
                        LabeledContent("Evaluation date", value: state.evaluation_date)
                        LabeledContent("Committed revision", value: String(state.revision))
                        Text("Updated \(state.generated_at)").font(.caption)
                        Text(model.connected ? "Local backend snapshot" : "Cached snapshot · freshness unverified").font(.caption)
                        if state.observations.contains(where: { $0.mode == "fixture" }) {
                            Label("Contains synthetic fixtures—not personal history", systemImage: "testtube.2").foregroundStyle(.orange)
                        }
                    }
                    Section("Ledger overview") {
                        ForEach(state.ledger, id: \.id) { card in
                            NavigationLink { LedgerDetail(model: model, cardID: card.id) } label: { CardSummary(card: card) }
                        }
                    }
                    if !model.hasPending {
                        Section("Optional suggestions") {
                            ForEach(state.ledger.filter { $0.suggestion?.status == "eligible" }, id: \.id) { card in
                                if let suggestion = card.suggestion {
                                    VStack(alignment: .leading, spacing: 8) {
                                        Text(suggestion.text)
                                        Text("\(card.mode) · rule \(card.rule_version)").font(.caption).foregroundStyle(.secondary)
                                        Button("Stop this suggestion", role: .destructive) {
                                            Task { await model.feedback(cardID: card.id, kind: "suppress", value: "stop", scope: "scenario") }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Section("Processing") {
                        Text("Ledger: \(state.process.ledger_status)")
                        Text("Synthesis: \(state.process.synthesis.mode) / \(state.process.synthesis.status)")
                        if let reason = state.process.synthesis.reason { Text(reason).font(.caption) }
                        Button("Recompute on backend (no AI)") { Task { await model.process(scripted: false) } }.disabled(model.hasPending)
                        Button("Run scripted demo synthesis") { Task { await model.process(scripted: true) } }
                            .disabled(model.hasPending || state.observations.contains { $0.mode == "live" })
                        ForEach(state.process.synthesis.drafts, id: \.id) { draft in
                            Text("Optional \(state.process.synthesis.mode) draft: \(draft.text)")
                        }
                    }
                    Section("Limitations") { ForEach(state.limitations, id: \.self) { Text($0) } }
                    if state.observations.isEmpty {
                        Section("Start deliberately") {
                            Text("Nothing is seeded or collected automatically. You may load synthetic demo records or connect HealthKit in Settings.")
                            Button("Load representative synthetic demo") { seedConfirmation = true }
                        }
                    }
                } else {
                    Section("No current snapshot") {
                        Text(model.snapshot.hidesCachedData ? "Reset/deletion pending. Old cached records are hidden until the server confirms the outcome." : "Start the local backend, then refresh. The app never silently substitutes fixtures for real data.")
                    }
                }
                Button("Refresh / retry saved requests") { Task { await model.refresh() } }
            }
            .disabled(model.busy)
            .navigationTitle("Today")
            .refreshable { await model.refresh() }
            .confirmationDialog("Load synthetic purchase and step fixtures? This is not consent to HealthKit or cloud AI.", isPresented: $seedConfirmation, titleVisibility: .visible) {
                Button("Load synthetic demo") { Task { await model.demo(action: "seed") } }
                Button("Cancel", role: .cancel) {}
            }
        }
    }
}

struct CardSummary: View {
    let card: LedgerCard
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(card.title).font(.headline)
            Text(card.observed)
            Text("\(card.mode == "fixture" ? "SYNTHETIC FIXTURE" : "LIVE RECORDS") · \(card.status) · coverage \(card.coverage)")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(.vertical, 3)
    }
}

struct LedgerView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        NavigationStack {
            List {
                if let state = model.state {
                    ForEach(state.ledger, id: \.id) { card in
                        NavigationLink { LedgerDetail(model: model, cardID: card.id) } label: { CardSummary(card: card) }
                    }
                } else { Text("Refresh Today to retrieve a ledger.") }
            }.navigationTitle("Ledger").refreshable { await model.refresh() }
        }
    }
}
