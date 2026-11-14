import SwiftUI
import CompanionCore

struct LedgerDetail: View {
    @ObservedObject var model: AppModel
    let cardID: String
    @SwiftUI.State private var feedbackSheet = false
    private var card: LedgerCard? { model.state?.ledger.first { $0.id == cardID } }
    var body: some View {
        List {
            if let card {
                Section("Observed, not assumed") { CardSummary(card: card) }
                if let inferred = card.inferred { Section("Backend interpretation") { Text(inferred) } }
                Section("Window and uncertainty") {
                    Text("\(card.window.start_date) through \(card.window.end_date)")
                    Text("Timezone: \(card.window.timezone)")
                    Text("Coverage: \(card.coverage)")
                    Text("Rule \(card.rule_version) · card revision \(card.revision)")
                    ForEach(card.limitations, id: \.self) { Text($0) }
                }
                Section("Exact evidence revisions") {
                    if card.evidence.isEmpty { Text("No supporting observations. This is not evidence of inactivity.") }
                    ForEach(card.evidence, id: \.observation_id) { ref in
                        if let o = model.state?.observations.first(where: { $0.id == ref.observation_id && $0.revision == ref.revision }) {
                            VStack(alignment: .leading, spacing: 5) {
                                Text("\(o.local_date) · \(o.source_id) · \(o.mode)").font(.headline)
                                if let activity = o.activity { Text("Recorded \(activity.value) \(activity.metric) (\(activity.unit))") }
                                if let purchase = o.purchase {
                                    Text("\(purchase.category ?? "Unknown category") · \(purchase.merchant ?? "Unknown merchant")")
                                    Text("Attributed to: \(o.household_attribution) · \(o.lifecycle)")
                                    Text("Authorized: \(purchase.authorized_date ?? "Unknown") · Posted: \(purchase.posted_date ?? "Unknown")").font(.caption)
                                    Text("Purchase context is not proof of eating or nutrition.").font(.caption)
                                }
                                Text("\(o.id) / revision \(o.revision)").font(.caption).textSelection(.enabled)
                                Text("\(o.provenance.adapter) v\(o.provenance.adapter_version) · \(o.provenance.method) · coverage \(o.provenance.coverage)").font(.caption)
                                Text("Timezone \(o.timezone) · precision \(o.time_precision)").font(.caption)
                            }
                        } else { Text("Referenced evidence is unavailable; refresh before drawing conclusions.") }
                    }
                }
                Section("Suggestion") {
                    if let suggestion = card.suggestion {
                        Text(model.hasPending ? "Pending changes: suggestion paused locally." : suggestion.text)
                        Text("Status: \(suggestion.status)")
                        if let reason = suggestion.reason { Text(reason).font(.caption) }
                        Button("Stop this scenario", role: .destructive) {
                            Task { await model.feedback(cardID: card.id, kind: "suppress", value: "stop", scope: "scenario") }
                        }
                    } else { Text("No compatible suggestion. Missing evidence is not confirmation.") }
                }
                Section("Optional feedback") {
                    Text("Correctness changes interpretation; usefulness only rates the suggestion. No answer is required.")
                    Button("Give correctness or usefulness feedback") { feedbackSheet = true }.disabled(model.hasPending)
                    ForEach(model.state?.feedback.filter { $0.card_id == cardID } ?? [], id: \.id) { feedback in
                        VStack(alignment: .leading) {
                            Text("\(feedback.kind): \(feedback.value) · \(feedback.scope)")
                            if !feedback.note.isEmpty { Text(feedback.note) }
                            Text(feedback.created_at).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            } else { Text("This card is no longer in the current epoch.") }
        }
        .disabled(model.busy)
        .navigationTitle(card?.title ?? "Ledger detail")
        .sheet(isPresented: $feedbackSheet) { if let card { FeedbackForm(model: model, card: card) } }
    }
}

struct FeedbackForm: View {
    @ObservedObject var model: AppModel
    @SwiftUI.State private var card: LedgerCard
    @SwiftUI.State private var epoch: String
    @Environment(\.dismiss) private var dismiss
    init(model: AppModel, card: LedgerCard) {
        self.model = model
        _card = SwiftUI.State(initialValue: card)
        _epoch = SwiftUI.State(initialValue: model.state?.data_epoch ?? "")
    }
    @SwiftUI.State private var kind = "correctness"
    @SwiftUI.State private var value = "correct"
    @SwiftUI.State private var eventID = ""
    @SwiftUI.State private var attribution = "unchanged"
    @SwiftUI.State private var note = ""
    private var ref: EvidenceRef? { card.evidence.first { $0.observation_id == eventID } }
    private var purchaseEvent: Bool { model.state?.observations.contains { $0.id == ref?.observation_id && $0.revision == ref?.revision && $0.kind == "purchase" } ?? false }
    var body: some View {
        NavigationStack {
            Form {
                Picker("Feedback type", selection: $kind) {
                    Text("Correctness").tag("correctness"); Text("Usefulness").tag("usefulness")
                }.onChange(of: kind) { _, new in value = new == "correctness" ? "correct" : "useful"; eventID = ""; attribution = "unchanged" }
                Picker("Verdict", selection: $value) {
                    if kind == "correctness" { Text("Correct").tag("correct"); Text("Incorrect").tag("incorrect") }
                    else { Text("Useful").tag("useful"); Text("Not useful").tag("not_useful") }
                    Text("Partially").tag("partial")
                }
                if kind == "correctness" {
                    Picker("Scope", selection: $eventID) {
                        Text("Whole card").tag("")
                        ForEach(card.evidence, id: \.observation_id) { ref in Text("\(ref.observation_id) r\(ref.revision)").tag(ref.observation_id) }
                    }.onChange(of: eventID) { _, _ in attribution = "unchanged" }
                    if purchaseEvent {
                        Picker("Purchase attribution", selection: $attribution) {
                            Text("Leave unchanged").tag("unchanged")
                            Text("Self").tag("self"); Text("Shared").tag("shared")
                            Text("Other person").tag("other"); Text("Unknown").tag("unknown")
                        }
                    }
                }
                TextField("Optional context (no private records)", text: $note, axis: .vertical)
                    .onChange(of: note) { _, new in if new.count > 1000 { note = String(new.prefix(1000)) } }
                Text("Original evidence remains immutable. Event corrections are a separate overlay. Unanswered feedback is never agreement.").font(.caption)
                Button("Save feedback") {
                    Task {
                        await model.feedback(cardID: card.id, kind: kind, value: value, scope: ref == nil ? "card" : "event", event: ref, attribution: purchaseEvent && attribution != "unchanged" ? attribution : nil, note: note, expectedEpoch: epoch, expectedCardRevision: card.revision)
                        dismiss()
                    }
                }.disabled(model.busy)
            }.navigationTitle("Optional feedback")
                .toolbar { Button("Cancel") { dismiss() } }
        }
    }
}
