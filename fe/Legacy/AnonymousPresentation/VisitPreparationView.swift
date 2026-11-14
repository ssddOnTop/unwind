import SwiftUI
import CompanionCore
#if canImport(UIKit)
import UIKit
#endif

/// Standalone value-driven UI. The parent supplies current presentation, a reset
/// token (change on account/epoch/reset), Ledger navigation, and explicit copy action.
/// No AppModel, network, cloud, automatic sharing, or clipboard reads are used here.
@MainActor
public struct VisitPreparationView: View {
    public let presentation: VisitPreparation
    public let resetToken: String
    private let onOpenLedger: () -> Void
    private let onCopy: (String) -> Bool
    /// The host updates the presentation immediately and persists answers locally
    /// by key. No callback means no question controls, not blocked inference.
    private let onClarification: ((VisitClarificationResponse) -> Void)?
    @State private var isExpanded = false
    @State private var isEditing = false
    @State private var wantsToClarify = false
    @State private var draft: VisitPreparationDraft
    @State private var copyStatus: String?

    public init(
        presentation: VisitPreparation,
        resetToken: String,
        onOpenLedger: @escaping () -> Void,
        onCopy: @escaping (String) -> Bool,
        onClarification: ((VisitClarificationResponse) -> Void)? = nil
    ) {
        self.presentation = presentation
        self.resetToken = resetToken
        self.onOpenLedger = onOpenLedger
        self.onCopy = onCopy
        self.onClarification = onClarification
        _draft = SwiftUI.State(initialValue: VisitPreparationDraft(presentation: presentation))
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if presentation.isDemo {
                Text(VisitPreparation.demoLabel)
                    .font(.caption.weight(.semibold))
                    .accessibilityIdentifier("visit.demoLabel")
            }
            Text(presentation.title)
                .font(.headline)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("visit.title")
            Text(presentation.caption)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("visit.caption")
            Button(isExpanded ? "Hide visit preparation" : "Prepare for a conversation") {
                isExpanded.toggle()
            }
            .accessibilityIdentifier("visit.expand")
            .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")

            if isExpanded {
                Divider()
                if let clarification = presentation.clarification,
                   (clarification.shouldPrompt || wantsToClarify), let onClarification {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Optional quick clarification")
                            .font(.headline)
                        Text(clarification.question)
                        Text(clarification.scope)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        HStack {
                            Button("Yes") {
                                onClarification(.init(key: clarification.key, answer: .yes))
                            }
                            .accessibilityIdentifier("visit.clarification.yes")
                            Button("No") {
                                onClarification(.init(key: clarification.key, answer: .no))
                            }
                            .accessibilityIdentifier("visit.clarification.no")
                            Button("Not sure") {
                                onClarification(.init(key: clarification.key, answer: .notSure))
                            }
                            .accessibilityIdentifier("visit.clarification.notSure")
                        }
                        Button("Skip this question") {
                            onClarification(.init(key: clarification.key, answer: .dismissed))
                        }
                        .accessibilityIdentifier("visit.clarification.skip")
                        Text("You can use the brief without answering or typing. An answer applies only to this purchase pattern.")
                            .font(.caption)
                    }
                    .accessibilityIdentifier("visit.clarification")
                    Divider()
                } else if presentation.clarification != nil, onClarification != nil {
                    Button("Change optional clarification") { wantsToClarify = true }
                        .accessibilityIdentifier("visit.clarification.change")
                }
                Text("Optional conversation preparation")
                    .font(.headline)
                    .accessibilityAddTraits(.isHeader)
                Text(presentation.reasonForDiscussion)
                Text("Where to start")
                    .font(.headline)
                    .accessibilityAddTraits(.isHeader)
                Text(VisitPreparation.doctorStartingPoint)
                Text(presentation.recordReviewLabel)
                    .font(.subheadline.weight(.semibold))
                Button("Review evidence in Ledger", action: onOpenLedger)
                    .accessibilityIdentifier("visit.ledger")

                Text("Your automatically prepared brief")
                    .font(.headline)
                    .accessibilityAddTraits(.isHeader)
                Text("No typing needed. Review the observed records, unconfirmed interpretation, and any optional context before copying.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button(isEditing ? "Finish editing" : "Edit brief (optional)") {
                    isEditing.toggle()
                }
                .accessibilityIdentifier("visit.edit")
                if isEditing {
                    Text("Edits stay in this view and reset when the underlying information changes.")
                        .font(.caption)
                    TextEditor(text: $draft.text)
                        .frame(minHeight: 280)
                        .padding(4)
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(.secondary.opacity(0.3)))
                        .accessibilityLabel("Editable visit brief")
                        .accessibilityHint("Editing is optional; observed records and inferred habits are separate from user-reported context")
                        .accessibilityIdentifier("visit.brief")
                        .onChange(of: draft.text) { _, _ in copyStatus = nil }
                } else {
                    Text(draft.text)
                        .font(.body)
                        .textSelection(.enabled)
                        .accessibilityIdentifier("visit.generatedBrief")
                }
                HStack {
                    Button("Reset draft") {
                        resetDraft()
                    }
                    .accessibilityIdentifier("visit.reset")
                    Spacer()
                    Button("Copy brief") {
                        // This is the only place onCopy is invoked. Merely opening,
                        // editing, or changing a presentation cannot copy or send.
                        copyStatus = onCopy(draft.textForCopy)
                            ? "Copied to clipboard — not sent to a doctor."
                            : "Could not copy. Your edited brief is still available here."
                    }
                    .disabled(draft.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("visit.copy")
                }
                if let copyStatus {
                    Text(copyStatus)
                        .font(.caption)
                        .accessibilityIdentifier("visit.copyStatus")
                }
                Text("Nothing is sent automatically. Copying places the brief on this device’s clipboard; review it before pasting anywhere.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(VisitPreparation.scopeNotice)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding()
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
        .onChange(of: presentation) { old, new in
            resetDraft()
            if old.clarification?.key != new.clarification?.key { isExpanded = false }
        }
        .onChange(of: resetToken) { _, _ in
            resetDraft()
            isExpanded = false
        }
    }

    private func resetDraft() {
        draft = VisitPreparationDraft(presentation: presentation)
        copyStatus = nil
        isEditing = false
        wantsToClarify = false
    }

    /// An optional iOS implementation for the parent's onCopy callback. localOnly
    /// prevents Universal Clipboard propagation; never call this during rendering.
    /// Other platforms must provide an equivalent local-only implementation.
    public static func copyToLocalClipboard(_ text: String) -> Bool {
        #if canImport(UIKit)
        UIPasteboard.general.setItems([["public.utf8-plain-text": text]], options: [.localOnly: true])
        return true
        #else
        return false
        #endif
    }
}

/// Opt-in gallery of presentation-only fixtures. Does not accept or mutate real
/// State. The parent decides where to mount this; it is not wired into the app.
@MainActor
public struct VisitPreparationDemoView: View {
    @State private var selection: VisitPreparation.Demo = .eatingRoutine
    @State private var clarificationResponse: VisitClarificationResponse?
    private let onOpenLedger: () -> Void
    private let onCopy: (String) -> Bool

    public init(onOpenLedger: @escaping () -> Void, onCopy: @escaping (String) -> Bool) {
        self.onOpenLedger = onOpenLedger
        self.onCopy = onCopy
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("DEMO visit preparation")
                .font(.title2.weight(.semibold))
            Text("Fictional presentations only. These examples do not change your connected information.")
                .font(.subheadline)
            Picker("Demo example", selection: $selection) {
                Text("Inferred eating-out routine").tag(VisitPreparation.Demo.eatingRoutine)
                Text("No concerns flagged").tag(VisitPreparation.Demo.noFlags)
                Text("Not enough information").tag(VisitPreparation.Demo.insufficientData)
            }
            .accessibilityIdentifier("visit.demoPicker")
            VisitPreparationView(
                presentation: .demo(selection, clarificationResponse: clarificationResponse),
                resetToken: selection.rawValue,
                onOpenLedger: onOpenLedger,
                onCopy: onCopy,
                onClarification: { clarificationResponse = $0 }
            )
        }
    }
}
