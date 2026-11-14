import SwiftUI

/// Supplies live transport callbacks to the completed server-driven component.
struct BankingScreen: View {
    @ObservedObject var controller: BankingController
    var body: some View {
        VStack(spacing: 0) {
            if let notice = controller.notice {
                Text(verbatim: notice).font(.callout).padding().frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("banking.action.result")
            }
            BankAccountsView(overview: controller.overview, status: controller.status,
                onRefresh: { Task { await controller.refresh() } },
                onAction: { controller.perform($0) })
        }
        .task { await controller.refresh() }
        .toolbar {
            if controller.hasLinkSession {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel setup") { controller.cancelLink() } }
            }
        }
    }
}
