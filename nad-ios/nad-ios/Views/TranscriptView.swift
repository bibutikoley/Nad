//
//  TranscriptView.swift
//  nad-ios
//
//  Renders Session.messages. Uses ScrollViewReader instead of List so bubbles can
//  stay flush with the conversational (large-radius) surface language rather than
//  stock List row chrome.
//

import LiveKit
import SwiftUI

struct TranscriptView: View {
    var messages: [ReceivedMessage]

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: NadTheme.Space.sm) {
                    ForEach(messages) { message in
                        TranscriptRow(message: message)
                            .id(message.id)
                    }
                }
                .padding(.horizontal, NadTheme.Space.md)
                .padding(.vertical, NadTheme.Space.sm)
            }
            .onChange(of: messages.last?.id) { _, newID in
                guard let newID else { return }
                withAnimation(NadTheme.Motion.state) {
                    proxy.scrollTo(newID, anchor: .bottom)
                }
            }
        }
    }
}

private struct TranscriptRow: View {
    let message: ReceivedMessage

    private var isFromUser: Bool {
        switch message.content {
        case .userTranscript, .userInput: true
        case .agentTranscript: false
        }
    }

    private var text: String {
        switch message.content {
        case let .agentTranscript(text), let .userTranscript(text), let .userInput(text):
            text
        }
    }

    var body: some View {
        HStack {
            if isFromUser { Spacer(minLength: NadTheme.Space.xxl) }

            Text(text)
                .font(NadTheme.Typography.body)
                .foregroundStyle(NadTheme.Color.bone)
                .padding(.horizontal, NadTheme.Space.md)
                .padding(.vertical, NadTheme.Space.sm)
                .background(
                    RoundedRectangle(cornerRadius: NadTheme.Radius.conversational, style: .continuous)
                        .fill(isFromUser ? NadTheme.Color.emberDim : NadTheme.Color.ink)
                )
                .opacity(message.isFinal ? 1 : 0.55)

            if !isFromUser { Spacer(minLength: NadTheme.Space.xxl) }
        }
        .animation(NadTheme.Motion.state, value: message.isFinal)
    }
}

#Preview {
    TranscriptView(messages: [
        ReceivedMessage(id: "1", timestamp: .now, content: .agentTranscript("Hey — I'm here. What's on your mind?"), isFinal: true),
        ReceivedMessage(id: "2", timestamp: .now, content: .userTranscript("Tell me about the weather"), isFinal: false),
    ])
    .background(NadTheme.Color.void)
}
