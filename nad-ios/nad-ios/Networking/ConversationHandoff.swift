//
//  ConversationHandoff.swift
//  nad-ios
//
//  Parks a past transcript on the token server so the agent can seed its chat context
//  with it and genuinely remember the conversation.
//
//  This goes through the token server rather than the join token because the token is
//  carried in the LiveKit connect URL's query string — a real conversation would bloat
//  it. Parking also happens *before* connecting, and connecting is what dispatches the
//  agent, so there is no race: the transcript is always there when the agent looks.
//

import Foundation

enum ConversationHandoffError: LocalizedError {
    case unauthorized
    case unreachable(Error)
    case rejected(Int)

    var errorDescription: String? {
        switch self {
        case .unauthorized:
            "The token server rejected this app's bearer token while saving context."
        case let .unreachable(underlying):
            "Couldn't reach the token server: \(underlying.localizedDescription)"
        case let .rejected(status):
            "The token server refused the conversation context (HTTP \(status))."
        }
    }
}

enum ConversationHandoff {

    /// Parks `conversation`'s transcript under `room`, which the caller must then join.
    static func park(_ conversation: Conversation, forRoom room: String, baseURL: URL) async throws {
        let body: [String: Any] = [
            "room": room,
            "messages": conversation.handoffPayload,
        ]

        var request = URLRequest(url: baseURL.appendingPathComponent("history"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(BackendConfig.authToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let response: URLResponse
        do {
            (_, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw ConversationHandoffError.unreachable(error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw ConversationHandoffError.rejected(0)
        }
        guard http.statusCode != 401 else { throw ConversationHandoffError.unauthorized }
        guard (200 ..< 300).contains(http.statusCode) else {
            throw ConversationHandoffError.rejected(http.statusCode)
        }
    }

    /// A room name the client picks itself. Resuming needs the name up front — before
    /// the token request — so the transcript can be parked under it first. Matches the
    /// token server's own `nad-<hex8>` shape.
    static func makeRoomName() -> String {
        "nad-\(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(8).lowercased())"
    }
}
