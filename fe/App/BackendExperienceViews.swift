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
                        Text("Patterns worth reviewing").font(.title2.bold())
                        Text(verbatim: patterns.source_label).font(.caption).foregroundStyle(.secondary)
                        Text("Only a pattern summary is sent for AI review.").font(.caption)
                        AccessibleInfoPopover(title: "Pattern privacy", message: patterns.privacy_notice)
                        Button(model.patternBusy ? "Analyzing patterns…" : "Analyze patterns") {
                            Task { await model.analyzePatterns() }
                        }.buttonStyle(.borderedProminent).disabled(model.busy || model.hasPending || !patterns.can_analyze)
                            .accessibilityIdentifier("patterns.analyze")
                        PatternSummaryView(model: model, value: patterns)
                        if !patterns.findings.isEmpty {
                            Button("Prepare an optional doctor question") { showVisit = true }
                            Button("Review saved patterns") { openLedger(nil) }
                        }
                    }
                    if let issue = model.patternMessage { Text(verbatim: issue).foregroundStyle(.red) }
                    if let value = model.experience, model.patternOverview?.is_demo != true {
                        if let sample = value.home.sample_label { Text(verbatim: sample).font(.caption).foregroundStyle(.secondary) }
                        HStack(alignment: .top) {
                            Text(verbatim: value.home.headline).font(.title2.bold())
                            Spacer()
                            if !value.home.info.isEmpty { AccessibleInfoPopover(title: "Your observation", message: value.home.info.joined(separator: "\n\n")) }
                        }
                        Text(verbatim: value.home.detail).font(.body)
                        if let action = value.home.primary_action, model.message == nil {
                            Button { route(action) } label: {
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(verbatim: action.label)
                                    if let detail = action.detail { Text(verbatim: detail).font(.caption) }
                                }
                            }.buttonStyle(.bordered).accessibilityIdentifier("experience.home.action")
                                .disabled(model.busy || model.hasPending)
                        }
                    } else if model.experience == nil {
                        Text("Your overview is not available yet.").font(.headline)
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
    @SwiftUI.State private var showQuestion = false
    @SwiftUI.State private var editing = false
    @SwiftUI.State private var editedBrief = ""
    @SwiftUI.State private var copyStatus: String?
    private func brief(_ value: ExperienceV1) -> String {
        guard let patterns = model.patternOverview, !patterns.doctor_brief.isEmpty else { return value.visit.brief }
        return patterns.is_demo ? patterns.doctor_brief : value.visit.brief + "\n\n" + patterns.doctor_brief
    }
    private var resetKey: String { "\(model.experience?.data_epoch ?? "none"):\(model.experience?.revision ?? 0):\(model.patternOverview?.run_id ?? "none"):\(model.patternOverview?.doctor_brief ?? "")" }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let value = model.experience {
                        Text(verbatim: value.visit.title).font(.title2.bold())
                        if let patterns = model.patternOverview, patterns.is_demo {
                            Text(verbatim: patterns.source_label).font(.caption)
                        } else if let sample = value.home.sample_label { Text(verbatim: sample).font(.caption) }
                        Text(verbatim: value.visit.caption)
                        AccessibleInfoPopover(title: "About this brief", message: value.visit.scope_notice + "\n\nEdits stay on this screen unless you tap Save notes to this review. Saved notes remain private, are not sent to AI, and do not alter sensor evidence. Context corrections reset edited notes to avoid keeping a stale interpretation. Copying uses only this device’s clipboard.")
                        if let question = value.clarification, model.patternOverview?.is_demo != true {
                            if question.should_prompt || showQuestion {
                                Text(verbatim: question.question).font(.headline)
                                Text(verbatim: question.scope).font(.caption).foregroundStyle(.secondary)
                                ForEach(question.options, id: \.value) { option in
                                    Button { Task { await model.answerExperience(option.value) } } label: { Text(verbatim: option.label) }
                                        .buttonStyle(.bordered).accessibilityIdentifier("experience.answer." + option.value)
                                        .disabled(!question.can_answer || model.busy || model.hasPending)
                                }
                            } else if question.can_answer {
                                Button("Review optional question") { showQuestion = true }
                            }
                        }
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
                        Button("Review inference history", action: openLedger)
                    } else {
                        Text("The overview changed or could not be loaded. Refresh before answering.")
                        Button("Refresh") { Task { await model.refresh() } }
                    }
                }.padding()
            }.accessibilityIdentifier("experience.visit.scroll")
                .navigationTitle("Your next step")
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
                .onChange(of: resetKey) { _, _ in editing = false; editedBrief = ""; copyStatus = nil; showQuestion = false }
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
    private func copy(_ experience: ExperienceV1) {
        var text = editing ? editedBrief : brief(experience)
        if model.patternOverview?.is_demo == true, !text.hasPrefix("SYNTHETIC HEALTH DEMO") {
            text = "SYNTHETIC HEALTH DEMO — not medical evidence.\n" + text
        }
        if experience.is_demo && model.patternOverview?.is_demo != true {
            // Preserve the server's mandatory visible sample marker even after cosmetic edits.
            guard let marker = experience.visit.brief.components(separatedBy: .newlines).first, !marker.isEmpty else { return }
            if !text.hasPrefix(marker) { text = marker + "\n" + text }
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
            ForEach(value.findings, id: \.concern_id) { finding in
                NavigationLink {
                    PatternFindingDetail(model: model, concernID: finding.concern_id)
                } label: {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(verbatim: finding.title).font(.headline)
                        Text(verbatim: finding.explanation).font(.subheadline)
                        Text("Evidence confidence: " + finding.confidence).font(.caption)
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                }.buttonStyle(.plain)
            }
            if value.status != "not_analyzed", value.findings.isEmpty {
                Text("No comparison patterns flagged in the available coverage. This is not a clean bill of health.")
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
                    Button("Were these records mostly about you?") { showContext.toggle() }
                    if showContext {
                        Button("Mostly about me") { Task { await model.clarifyPattern(concernID, attribution: "for_me") } }
                        Button("Not mostly about me") { Task { await model.clarifyPattern(concernID, attribution: "not_for_me") } }
                        Button("Not sure") { Task { await model.clarifyPattern(concernID, attribution: "unsure") } }
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
