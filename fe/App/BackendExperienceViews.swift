import SwiftUI
import UIKit
import CompanionCore

/// A renderer, not a rules engine: strings, availability and routing metadata are supplied by the server.
struct BackendHomeView: View {
    @ObservedObject var model: AppModel
    let openConnections: () -> Void
    let openLedger: (String?) -> Void
    @SwiftUI.State private var showVisit = false
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let issue = model.message {
                        Text(verbatim: issue).foregroundStyle(.red)
                        Button("Try again") { Task { await model.refresh() } }
                    }
                    if let patterns = model.patternOverview {
                        if let page = model.workflowPage("home") {
                            Text(verbatim: page.title).font(.title2.bold())
                            ForEach(page.paragraphs, id: \.self) { Text(verbatim: $0).font(.caption) }
                        }
                        Text(verbatim: patterns.source_label).font(.caption).foregroundStyle(.secondary)
                        AccessibleInfoPopover(title: "Pattern privacy", message: patterns.privacy_notice)
                        if let action = model.workflowOverview?.action {
                            Button(action.label) { Task { await model.analyzePatterns() } }
                                .buttonStyle(.borderedProminent).disabled(model.busy || model.hasPending)
                                .accessibilityIdentifier("patterns.enqueue")
                        }
                        if let job = model.workflowOverview?.job {
                            Text(verbatim: job.message).font(.caption)
                            Text(verbatim: job.bank_message).font(.caption)
                            if job.status == "queued" || job.status == "running" { ProgressView() }
                        }
                        PatternSummaryView(model: model, value: patterns)
                        if !patterns.findings.isEmpty {
                            Button(model.workflowPage("brief")?.title ?? "Review notes") { showVisit = true }
                            Button("Review saved patterns") { openLedger(nil) }
                        }
                    }
                    if let issue = model.patternMessage { Text(verbatim: issue).foregroundStyle(.red) }
                    if model.patternOverview == nil {
                        Text("Your current assessment is not available yet.").font(.headline)
                        Text("Refresh when your secure service is available.").foregroundStyle(.secondary)
                    }
                    if model.busy { ProgressView("Updating…") }
                }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
            }.navigationTitle("Today").accessibilityIdentifier("app.home")
                .refreshable { await model.refresh() }
                .sheet(isPresented: $showVisit) {
                    BackendVisitView(model: model, openLedger: { showVisit = false; openLedger(nil) })
                }
        }
    }
    private func route(_ action: CompanionCore.ExperienceAction) {
        switch action.kind {
        case "connect": openConnections()
        case "view_visit": showVisit = true
        case "view_ledger", "explore_suggestion": openLedger(action.card_id)
        default: break // Strict decoding rejects unrecognized actions; no executable server text.
        }
    }
}

struct BackendVisitView: View {
    @ObservedObject var model: AppModel
    let openLedger: () -> Void
    @Environment(\.dismiss) private var dismiss
    @SwiftUI.State private var expanded = false
    @SwiftUI.State private var editing = false
    @SwiftUI.State private var editedBrief = ""
    @SwiftUI.State private var copyStatus: String?
    private func brief(_ value: PatternOverview) -> String {
        guard let patterns = model.patternOverview, patterns.is_current else { return "No current assessment is available." }
        return patterns.doctor_brief
    }
    private var resetKey: String { "\(model.experience?.data_epoch ?? "none"):\(model.experience?.revision ?? 0):\(model.patternOverview?.run_id ?? "none"):\(model.patternOverview?.doctor_brief ?? "")" }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let value = model.patternOverview, value.is_current {
                        Text(model.workflowPage("brief")?.title ?? "Current assessment notes").font(.title2.bold())
                        Text(verbatim: value.source_label).font(.caption)
                        Text("Notes for your latest assessment, not a diagnosis.")
                        AccessibleInfoPopover(title: "About this brief", message: value.privacy_notice + "\n\nEdits stay on this screen unless you tap Save notes to this review. Saved notes remain private, are not sent to AI, and do not alter sensor evidence. Context corrections append a new revision; earlier notes remain in private history. Copying uses only this device’s clipboard.")
                        if let message = model.message { Text(verbatim: message).foregroundStyle(.red) }
                        Button(expanded ? "Close brief" : "Read your brief") { expanded.toggle() }.accessibilityIdentifier("experience.visit.expand")
                        if expanded {
                            // The brief already contains these sections; do not
                            // render every warning and observation twice.
                            if editing {
                                TextEditor(text: $editedBrief).frame(minHeight: 240).accessibilityIdentifier("experience.visit.edit")
                            } else { Text(verbatim: brief(value)).textSelection(.enabled).accessibilityIdentifier("experience.visit.brief") }
                            Button(editing ? "Use original brief" : "Edit a copy") { editing.toggle(); editedBrief = brief(value) }
                            if editing, model.patternOverview?.run_id != nil {
                                Button("Save notes to this review") { Task { if await model.savePatternBrief(editedBrief) { editing = false } } }
                                    .disabled(model.busy || editedBrief.isEmpty || editedBrief.count > 4000)
                            }
                            if let issue = model.patternMessage { Text(verbatim: issue).foregroundStyle(.red) }
                            Button("Copy brief") { copy(value) }.accessibilityIdentifier("experience.visit.copy")
                            if let copyStatus { Text(verbatim: copyStatus).font(.caption).accessibilityIdentifier("experience.visit.copyStatus") }
                        }
                        Button("Review current assessment", action: openLedger)
                    } else {
                        Text("The overview changed or could not be loaded. Refresh before answering.")
                        Button("Refresh") { Task { await model.refresh() } }
                    }
                }.padding()
            }.accessibilityIdentifier("experience.visit.scroll")
                .navigationTitle("Your next step")
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                .onChange(of: resetKey) { _, _ in editing = false; editedBrief = ""; copyStatus = nil }
        }
    }
    private func lines(_ title: String, _ values: [String]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !values.isEmpty {
                Text(title).font(.headline)
                ForEach(Array(values.enumerated()), id: \.offset) { _, line in Text(verbatim: line) }
            }
        }
    }
    private func copy(_ experience: PatternOverview) {
        var text = editing ? editedBrief : brief(experience)
        if model.patternOverview?.is_demo == true, !text.hasPrefix("SYNTHETIC HEALTH DEMO") {
            text = "SYNTHETIC HEALTH DEMO — not medical evidence.\n" + text
        }
        UIPasteboard.general.setItems([["public.utf8-plain-text": text]], options: [.localOnly: true, .expirationDate: Date().addingTimeInterval(300)])
        copyStatus = "Copied on this device only; not sent."
    }
}

/// The backend supplies every finding and its approved evidence wording.
struct PatternSummaryView: View {
    @ObservedObject var model: AppModel
    let value: PatternOverview
    var separateListRows = false
    var body: some View {
        if separateListRows {
            // Each link must be its own List row, not several links in one cell.
            rows
        } else {
            VStack(alignment: .leading, spacing: 12) { rows }
        }
    }
    @ViewBuilder private var rows: some View {
            if value.status == "unavailable" || value.status == "running" {
                Text("AI review unavailable — deterministic findings remain available.").font(.caption).foregroundStyle(.secondary)
            }
            if let at = value.analyzed_at { Text("Analyzed: " + at).font(.caption).foregroundStyle(.secondary) }
            if value.status != "not_analyzed" { Text(value.engine == "LLMassisted" ? "AI-ranked, evidence-checked" : "Deterministic review").font(.caption) }
            if value.is_current {
                ForEach(value.areas, id: \.area) { area in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(area.area == "food_service" ? "Eating out" : area.area.capitalized).font(.headline)
                        Text(verbatim: area.summary).font(.subheadline)
                        if area.state == "concern" {
                            NavigationLink("Optional explanation") {
                                PatternFindingDetail(model: model, concernID: area.area)
                            }.buttonStyle(.plain)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 8)
                }
            } else if value.status != "not_analyzed" {
                Text("A current assessment is being prepared. Earlier warnings are not shown as current.")
            }

    }
}

private struct PatternFindingDetail: View {
    @ObservedObject var model: AppModel
    let concernID: String
    @SwiftUI.State private var showContext = false
    private var finding: PatternFinding? { model.patternOverview?.findings.first { $0.concern_id == concernID } }
    var body: some View {
        List {
            if let finding, let overview = model.patternOverview {
                Section("What and why") {
                    Text(verbatim: finding.explanation)
                    Text(verbatim: finding.evidence_summary)
                    Text("Evidence confidence: " + finding.confidence)
                    Text("Context: " + finding.attribution.replacingOccurrences(of: "_", with: " "))
                }
                Section("When and source") {
                    Text(verbatim: overview.source_label)
                    if let at = overview.analyzed_at { Text(verbatim: at) }
                    Text(verbatim: overview.engine)
                    Text("Evidence codes: " + finding.evidence_codes.joined(separator: ", ")).font(.caption)
                }
                Section("Limitations") {
                    Text(verbatim: overview.privacy_notice)
                    if let evidence = overview.evidence {
                        Text("Recent missing coverage flags — steps: \(evidence.recent.missing_steps ? "yes" : "no"); sleep: \(evidence.recent.missing_sleep ? "yes" : "no"); transactions: \(evidence.recent.missing_transactions ? "yes" : "no").")
                    }
                }
                Section("Optional context") {
                    Button(model.workflowOverview?.questions.first?.label ?? "Optional context") { showContext.toggle() }
                    if showContext {
                        ForEach(model.workflowOverview?.questions.first?.choices ?? [], id: \.id) { choice in
                            Button(choice.label) { Task { await model.clarifyPattern(concernID, attribution: choice.id) } }
                        }
                    }
                    Text("Your choice is saved only when tapped. It is not sent for AI review.").font(.caption)
                }.disabled(model.busy)
            } else { Text("This pattern is no longer available. Refresh the overview.") }
        }.navigationTitle(finding?.title ?? "Pattern")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text(verbatim: finding?.title ?? "Pattern")
                        .font(.headline)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("pattern.finding.title")
                }
            }
    }
}
