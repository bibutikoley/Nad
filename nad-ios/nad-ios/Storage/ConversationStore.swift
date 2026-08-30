//
//  ConversationStore.swift
//  nad-ios
//
//  Durable conversation history, kept on this device only — nothing is uploaded except
//  the one transcript handed to the agent when you explicitly resume a conversation.
//
//  Writes are debounced because the live session upserts on every partial transcript
//  (several times a second while the agent speaks), but they are also flushed on
//  disconnect and on backgrounding so an app kill can't lose a conversation.
//

import Combine
import Foundation

@MainActor
final class ConversationStore: ObservableObject {

    /// Newest first — the order the history list wants.
    @Published private(set) var conversations: [Conversation] = []

    private let fileURL: URL
    private var pendingWrite: Task<Void, Never>?
    private let writeDebounce: Duration = .milliseconds(800)

    init(filename: String = "conversations.json") {
        let directory = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Nad", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        fileURL = directory.appendingPathComponent(filename)
        load()
    }

    // MARK: - Mutation

    /// Inserts or replaces a conversation, keeping the list newest-first. Conversations
    /// with nothing said in them are dropped rather than cluttering the list.
    func upsert(_ conversation: Conversation) {
        guard conversation.isWorthKeeping else {
            if let index = conversations.firstIndex(where: { $0.id == conversation.id }) {
                conversations.remove(at: index)
                scheduleWrite()
            }
            return
        }

        if let index = conversations.firstIndex(where: { $0.id == conversation.id }) {
            guard conversations[index] != conversation else { return }
            conversations[index] = conversation
        } else {
            conversations.append(conversation)
        }
        conversations.sort { $0.updatedAt > $1.updatedAt }
        scheduleWrite()
    }

    func delete(_ id: Conversation.ID) {
        conversations.removeAll { $0.id == id }
        scheduleWrite()
    }

    func deleteAll() {
        conversations.removeAll()
        scheduleWrite()
    }

    // MARK: - Persistence

    private func load() {
        guard let data = try? Data(contentsOf: fileURL) else { return }
        do {
            let decoded = try JSONDecoder().decode([Conversation].self, from: data)
            conversations = decoded.sorted { $0.updatedAt > $1.updatedAt }
        } catch {
            // A corrupt or outdated file shouldn't take the app down or wipe itself
            // silently — start empty and leave the file alone for inspection.
            assertionFailure("Could not decode conversation history: \(error)")
        }
    }

    private func scheduleWrite() {
        pendingWrite?.cancel()
        let snapshot = conversations
        let url = fileURL
        let delay = writeDebounce
        pendingWrite = Task {
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled else { return }
            await Self.write(snapshot, to: url)
        }
    }

    /// Writes immediately, cancelling any debounced write. Call when the session ends
    /// or the app is about to be backgrounded.
    func flush() async {
        pendingWrite?.cancel()
        pendingWrite = nil
        await Self.write(conversations, to: fileURL)
    }

    private nonisolated static func write(_ conversations: [Conversation], to url: URL) async {
        await Task.detached(priority: .utility) {
            do {
                let encoder = JSONEncoder()
                encoder.outputFormatting = .withoutEscapingSlashes
                let data = try encoder.encode(conversations)
                // Atomic: a crash mid-write must not leave a truncated file that
                // would fail to decode and drop the user's whole history.
                try data.write(to: url, options: .atomic)
            } catch {
                assertionFailure("Could not save conversation history: \(error)")
            }
        }.value
    }
}
