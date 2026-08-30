//
//  NadTokenSource.swift
//  nad-ios
//
//  Bridges nad-backend's token_server.py to the LiveKit SDK's TokenSourceFixed
//  protocol. The response shape ({"url","room","token"}, GET + bearer header) does
//  not match LiveKit's standard endpoint format ({"serverUrl","participantToken"}),
//  so the SDK's built-in EndpointTokenSource can't be pointed at it directly —
//  this type does the translation.
//
//  Resolves AppSettings lazily on every fetch(), so one Session/NadTokenSource
//  survives a Settings edit (e.g. a new LAN IP) with no teardown needed.
//

import Foundation
import LiveKit

enum NadTokenSourceError: LocalizedError {
    case invalidServerURL(String)
    case unauthorized
    case unreachable(Underlying: Error)
    case malformedResponse(String)
    case invalidLiveKitURL(String)

    var errorDescription: String? {
        switch self {
        case let .invalidServerURL(value):
            "'\(value)' isn't a valid server URL. Check the override in Settings."
        case .unauthorized:
            "The token server rejected this app's bearer token. It doesn't match TOKEN_SERVER_AUTH_TOKEN in nad-backend/.env — update BackendConfig.swift."
        case let .unreachable(underlying):
            "Couldn't reach the token server: \(underlying.localizedDescription)"
        case let .malformedResponse(detail):
            "The token server's response didn't look right: \(detail)"
        case let .invalidLiveKitURL(value):
            "The token server returned an invalid LiveKit URL: '\(value)'"
        }
    }
}

private nonisolated struct TokenEndpointResponse: Decodable {
    let url: String
    let room: String
    let token: String
}

struct NadTokenSource: TokenSourceConfigurable {
    let settings: AppSettings

    /// - Important: Never call `Session.withAgent(_:)` with this source — that sets
    /// `agentName` in `TokenRequestOptions`, switching LiveKit to explicit agent
    /// dispatch. `agent.py` registers with no agent name (implicit dispatch), so an
    /// explicitly-dispatched request would never be joined. Use plain
    /// `Session(tokenSource:tokenOptions:)` instead.
    func fetch(_ options: TokenRequestOptions) async throws -> TokenSourceResponse {
        // AppSettings is @MainActor; TokenSourceConfigurable.fetch is called from a
        // nonisolated context, so every read of `settings` is gathered in one hop.
        let (baseURL, room, identity) = await MainActor.run {
            (
                settings.effectiveBaseURL,
                options.roomName ?? settings.effectiveRoomName,
                options.participantIdentity ?? settings.effectiveParticipantIdentity
            )
        }

        var components = URLComponents(url: baseURL.appendingPathComponent("token"), resolvingAgainstBaseURL: false)
        var query: [URLQueryItem] = []
        if let room {
            query.append(URLQueryItem(name: "room", value: room))
        }
        if let identity {
            query.append(URLQueryItem(name: "identity", value: identity))
        }
        components?.queryItems = query.isEmpty ? nil : query

        guard let url = components?.url else {
            throw NadTokenSourceError.invalidServerURL(baseURL.absoluteString)
        }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer \(BackendConfig.authToken)", forHTTPHeaderField: "Authorization")

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw NadTokenSourceError.unreachable(Underlying: error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw NadTokenSourceError.malformedResponse("no HTTP status")
        }
        guard http.statusCode != 401 else {
            throw NadTokenSourceError.unauthorized
        }
        guard (200 ..< 300).contains(http.statusCode) else {
            throw NadTokenSourceError.malformedResponse("HTTP \(http.statusCode)")
        }

        let decoded: TokenEndpointResponse
        do {
            decoded = try JSONDecoder().decode(TokenEndpointResponse.self, from: data)
        } catch {
            throw NadTokenSourceError.malformedResponse(error.localizedDescription)
        }

        guard let liveKitURL = URL(string: decoded.url), liveKitURL.scheme != nil else {
            throw NadTokenSourceError.invalidLiveKitURL(decoded.url)
        }

        return TokenSourceResponse(
            serverURL: liveKitURL,
            participantToken: decoded.token,
            roomName: decoded.room
        )
    }
}
