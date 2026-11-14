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
                    if let value = model.experience {
                        if let sample = value.home.sample_label { Text(verbatim: sample).font(.caption).foregroundStyle(.secondary) }
                        HStack(alignment: .top) {
                            Text(verbatim: value.home.headline).font(.title2.bold())
                            Spacer()
                            if !value.home.info.isEmpty { AccessibleInfoPopover(title: "Your observation", message: value.home.info.joined(separator: "\n\n")) }
                        }
                        Text(verbatim: value.home.detail).font(.body)
                        if let action = value.home.primary_action {
                            Button { route(action) } label: {
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(verbatim: action.label)
                                    if let detail = action.detail { Text(verbatim: detail).font(.caption) }
                                }
                            }.buttonStyle(.borderedProminent).accessibilityIdentifier("experience.home.action")
                                .disabled(model.busy || model.hasPending)
                        }
                    } else {
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
    private var resetKey: String { "\(model.experience?.data_epoch ?? "none"):\(model.experience?.revision ?? 0)" }
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let value = model.experience {
                        Text(verbatim: value.visit.title).font(.title2.bold())
                        if let sample = value.home.sample_label { Text(verbatim: sample).font(.caption) }
                        Text(verbatim: value.visit.caption)
                        AccessibleInfoPopover(title: "About this brief", message: value.visit.scope_notice + "\n\nOptional text edits are local to this screen and never change your records. They reset when the overview changes. Copying uses only this device’s clipboard.")
                        if let question = value.clarification {
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
                            Text(verbatim: value.visit.reason_for_discussion)
                            Text(verbatim: value.visit.doctor_starting_point)
                            lines("Observed records", value.visit.observed_information)
                            lines("Possible patterns", value.visit.inferred_habits)
                            lines("Your confirmed context", value.visit.confirmed_context)
                            lines("Your preferences", value.visit.user_reported_preferences)
                            lines("Uncertainties", value.visit.uncertainties)
                            lines("Questions to discuss", value.visit.questions)
                            if editing {
                                TextEditor(text: $editedBrief).frame(minHeight: 240).accessibilityIdentifier("experience.visit.edit")
                            } else { Text(verbatim: value.visit.brief).textSelection(.enabled).accessibilityIdentifier("experience.visit.brief") }
                            Button(editing ? "Use original brief" : "Edit a copy") { editing.toggle(); editedBrief = value.visit.brief }
                            Button("Copy brief") { copy(value) }.accessibilityIdentifier("experience.visit.copy")
                            if let copyStatus { Text(verbatim: copyStatus).font(.caption).accessibilityIdentifier("experience.visit.copyStatus") }
                        }
                        Button("Review evidence in Ledger", action: openLedger)
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
        var text = editing ? editedBrief : experience.visit.brief
        if experience.is_demo {
            // Preserve the server's mandatory visible sample marker even after cosmetic edits.
            guard let marker = experience.visit.brief.components(separatedBy: .newlines).first, !marker.isEmpty else { return }
            if !text.hasPrefix(marker) { text = marker + "\n" + text }
        }
        UIPasteboard.general.setItems([["public.utf8-plain-text": text]], options: [.localOnly: true, .expirationDate: Date().addingTimeInterval(300)])
        copyStatus = "Copied on this device only; not sent."
    }
}
