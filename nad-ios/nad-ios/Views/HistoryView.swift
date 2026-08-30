//
//  HistoryView.swift
//  nad-ios
//
//  Saved conversations, rendered in Liquid Glass: each row is its own glass card inside
//  a GlassEffectContainer, so the whole list rasterizes in one pass. Deletion is a
//  context menu on a row rather than a swipe action, since these aren't List rows.
//

import SwiftUI

struct HistoryView: View {
    @ObservedObject var store: ConversationStore
    /// Resume the tapped conversation: reconnects with its transcript handed to the
    /// agent so it actually remembers what was said.
    var onResume: (Conversation) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var confirmingClearAll = false
    /// Keyed by id rather than the value: `ReceivedMessage` isn't Hashable, so
    /// `Conversation` can't be either, and `navigationDestination(item:)` requires it.
    @State private var viewingID: Conversation.ID?

    var body: some View {
        NavigationStack {
            Group {
                if store.conversations.isEmpty {
                    empty
                } else {
                    list
                }
            }
            .background(NadTheme.Color.void)
            .navigationTitle("History")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                        .foregroundStyle(NadTheme.Color.mist)
                }
                if !store.conversations.isEmpty {
                    ToolbarItem(placement: .primaryAction) {
                        Button("Clear All", role: .destructive) { confirmingClearAll = true }
                            .foregroundStyle(NadTheme.Color.fault)
                    }
                }
            }
            .confirmationDialog(
                "Delete all \(store.conversations.count) conversations?",
                isPresented: $confirmingClearAll,
                titleVisibility: .visible
            ) {
                Button("Delete All", role: .destructive) { store.deleteAll() }
                Button("Cancel", role: .cancel) { }
            } message: {
                Text("This can't be undone.")
            }
            .navigationDestination(item: $viewingID) { id in
                if let conversation = store.conversations.first(where: { $0.id == id }) {
                    ConversationDetailView(conversation: conversation) {
                        dismiss()
                        onResume(conversation)
                    }
                }
            }
        }
        .preferredColorScheme(.dark)
    }

    private var empty: some View {
        VStack(spacing: NadTheme.Space.sm) {
            Image(systemName: "clock.arrow.circlepath")
                .font(.system(size: 28, weight: .light))
                .foregroundStyle(NadTheme.Color.mist)
                .frame(width: 76, height: 76)
                .glassEffect(.regular, in: Circle())
                .padding(.bottom, NadTheme.Space.xs)
            Text("No conversations yet")
                .font(NadTheme.Typography.body)
                .foregroundStyle(NadTheme.Color.mist)
            Text("Sessions are saved here automatically, on this device only.")
                .font(NadTheme.Typography.data)
                .foregroundStyle(NadTheme.Color.mist.opacity(0.7))
                .multilineTextAlignment(.center)
        }
        .padding(NadTheme.Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var list: some View {
        ScrollView {
            GlassEffectContainer(spacing: 0) {
                LazyVStack(spacing: NadTheme.Space.xs) {
                    ForEach(store.conversations) { conversation in
                        Button {
                            viewingID = conversation.id
                        } label: {
                            HistoryRow(conversation: conversation)
                        }
                        .buttonStyle(.plain)
                        .glassEffect(
                            .regular.interactive(),
                            in: RoundedRectangle(
                                cornerRadius: NadTheme.Radius.conversational,
                                style: .continuous
                            )
                        )
                        .contextMenu {
                            Button("Resume") {
                                dismiss()
                                onResume(conversation)
                            }
                            Button("Delete", role: .destructive) {
                                store.delete(conversation.id)
                            }
                        }
                    }
                }
            }
            .padding(.horizontal, NadTheme.Space.md)
            .padding(.vertical, NadTheme.Space.xs)
        }
        .scrollEdgeEffectStyle(.soft, for: .top)
    }
}

private struct HistoryRow: View {
    let conversation: Conversation

    var body: some View {
        HStack(spacing: NadTheme.Space.sm) {
            VStack(alignment: .leading, spacing: NadTheme.Space.xxs) {
                Text(conversation.title)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .font(NadTheme.Typography.body)
                    .foregroundStyle(NadTheme.Color.bone)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)

                Text("\(Self.stamp(conversation.updatedAt)) · \(conversation.turnCount) messages")
                    .font(NadTheme.Typography.data)
                    .foregroundStyle(NadTheme.Color.mist)
            }

            Spacer(minLength: 0)

            Image(systemName: "chevron.right")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(NadTheme.Color.graphite)
        }
        .padding(.horizontal, NadTheme.Space.md)
        .padding(.vertical, NadTheme.Space.sm)
        .contentShape(Rectangle())
    }

    static func stamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        if Calendar.current.isDateInToday(date) {
            formatter.dateFormat = "HH:mm"
            return "Today \(formatter.string(from: date))"
        }
        if Calendar.current.isDateInYesterday(date) {
            formatter.dateFormat = "HH:mm"
            return "Yesterday \(formatter.string(from: date))"
        }
        formatter.dateFormat = "d MMM, HH:mm"
        return formatter.string(from: date)
    }
}

/// Read-only transcript, with the option to pick the conversation back up.
private struct ConversationDetailView: View {
    let conversation: Conversation
    var onResume: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            TranscriptView(messages: conversation.messages)

            Button(action: onResume) {
                HStack(spacing: NadTheme.Space.xs) {
                    Image(systemName: "arrow.uturn.backward")
                    Text("Resume conversation")
                }
                .font(NadTheme.Typography.bodyEmphasis)
                .frame(maxWidth: .infinity)
                .padding(.vertical, NadTheme.Space.xs)
            }
            .buttonStyle(.glassProminent)
            .tint(NadTheme.Color.ember)
            .controlSize(.large)
            .padding(.horizontal, NadTheme.Space.md)
            .padding(.bottom, NadTheme.Space.md)
        }
        .background(NadTheme.Color.void)
        .navigationTitle(HistoryRow.stamp(conversation.updatedAt))
        .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview {
    HistoryView(store: ConversationStore(filename: "preview.json")) { _ in }
}
