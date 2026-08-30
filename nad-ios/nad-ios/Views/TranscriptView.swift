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

    /// A streaming reply mutates the *same* message, so its id never changes and an
    /// id-keyed onChange would let a long turn grow off the bottom of the screen.
    /// Keying on the text length too makes the view follow the reply as it arrives.
    private var tail: String {
        guard let last = messages.last else { return "" }
        return "\(last.id)-\(lastText.count)-\(last.isFinal)"
    }

    private var lastText: String {
        switch messages.last?.content {
        case let .agentTranscript(text), let .userTranscript(text), let .userInput(text):
            text
        case nil:
            ""
        }
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                // One container renders every bubble's glass in a single pass. spacing 0
                // keeps neighbouring bubbles from blending into each other — merging is
                // for controls that belong together, not for separate utterances.
                GlassEffectContainer(spacing: 0) {
                    LazyVStack(alignment: .leading, spacing: NadTheme.Space.sm) {
                        ForEach(messages) { message in
                            TranscriptRow(message: message)
                                .id(message.id)
                        }
                    }
                }
                .padding(.horizontal, NadTheme.Space.md)
                .padding(.vertical, NadTheme.Space.sm)
            }
            .defaultScrollAnchor(.bottom)
            // Content dissolves under the header rather than colliding with it.
            .scrollEdgeEffectStyle(.soft, for: .top)
            .onChange(of: tail) { _, _ in
                guard let lastID = messages.last?.id else { return }
                withAnimation(NadTheme.Motion.state) {
                    proxy.scrollTo(lastID, anchor: .bottom)
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
                // Liquid Glass: the user's own words carry the ember tint, the agent's
                // stay untinted, so the two voices still read apart at a glance.
                .glassEffect(
                    isFromUser ? .regular.tint(NadTheme.Color.ember.opacity(0.32)) : .regular,
                    in: RoundedRectangle(cornerRadius: NadTheme.Radius.conversational, style: .continuous)
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
