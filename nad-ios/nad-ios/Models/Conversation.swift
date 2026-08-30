//
//  Conversation.swift
//  nad-ios
//
//  One saved session. Stores LiveKit's own `ReceivedMessage` values rather than a
//  parallel message type: it is already Codable, so the transcript round-trips to disk
//  and back into `Session.restoreMessageHistory(_:)` with no lossy translation.
//

import Foundation
import LiveKit

struct Conversation: Identifiable, Codable, Equatable {
    let id: UUID
    let startedAt: Date
    var updatedAt: Date
    var messages: [ReceivedMessage]

    init(id: UUID = UUID(), startedAt: Date = Date(), updatedAt: Date = Date(), messages: [ReceivedMessage] = []) {
        self.id = id
        self.startedAt = startedAt
        self.updatedAt = updatedAt
        self.messages = messages
    }
}

extension Conversation {
    /// What the user said first — the most useful handle on a voice conversation, since
    /// the agent's opener is always some variant of the same greeting.
    var title: String {
        let opener = messages.first { $0.isFromUser && !$0.text.isEmpty }
            ?? messages.first { !$0.text.isEmpty }
        guard let text = opener?.text, !text.isEmpty else { return "Empty conversation" }
        return text
    }

    var turnCount: Int {
        messages.filter { !$0.text.isEmpty }.count
    }

    var isWorthKeeping: Bool {
        messages.contains { !$0.text.isEmpty }
    }

    /// The shape the token server parks for the agent to collect.
    var handoffPayload: [[String: String]] {
        messages.compactMap { message in
            let text = message.text
            guard !text.isEmpty else { return nil }
            return ["role": message.isFromUser ? "user" : "assistant", "text": text]
        }
    }
}

extension ReceivedMessage {
    var isFromUser: Bool {
        switch content {
        case .userTranscript, .userInput: true
        case .agentTranscript: false
        }
    }

    var text: String {
        switch content {
        case let .agentTranscript(text), let .userTranscript(text), let .userInput(text):
            text.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }
}
