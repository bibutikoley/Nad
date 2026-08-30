//
//  ComposerView.swift
//  nad-ios
//

import SwiftUI

struct ComposerView: View {
    var onSend: (String) async -> Void

    @State private var draft = ""
    @FocusState private var isFocused: Bool

    var body: some View {
        HStack(spacing: NadTheme.Space.sm) {
            TextField("Type instead of talking…", text: $draft, axis: .vertical)
                .font(NadTheme.Typography.body)
                .foregroundStyle(NadTheme.Color.bone)
                .tint(NadTheme.Color.ember)
                .focused($isFocused)
                .lineLimit(1 ... 4)
                .padding(.horizontal, NadTheme.Space.md)
                .padding(.vertical, NadTheme.Space.sm)
                .background(
                    RoundedRectangle(cornerRadius: NadTheme.Radius.conversational, style: .continuous)
                        .fill(NadTheme.Color.ink)
                        .overlay(
                            RoundedRectangle(cornerRadius: NadTheme.Radius.conversational, style: .continuous)
                                .stroke(isFocused ? NadTheme.Color.ember.opacity(0.5) : NadTheme.Color.graphite, lineWidth: 1)
                        )
                )

            Button {
                let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else { return }
                draft = ""
                Task { await onSend(text) }
            } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(NadTheme.Color.void)
                    .frame(width: 34, height: 34)
                    .background(Circle().fill(canSend ? NadTheme.Color.ember : NadTheme.Color.graphite))
            }
            .disabled(!canSend)
            .animation(NadTheme.Motion.reaction, value: canSend)
        }
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

#Preview {
    ComposerView(onSend: { _ in })
        .padding()
        .background(NadTheme.Color.void)
}
