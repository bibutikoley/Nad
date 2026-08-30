//
//  AppSettings.swift
//  nad-ios
//
//  Runtime-editable overrides, distinct from BackendConfig.swift's compiled-in
//  defaults. Only the base URL is worth changing on-device: nad-backend's LAN IP
//  churns on DHCP renewal (see nad-backend/README.md), so being able to fix it
//  from Settings without a rebuild is the whole point of this screen. The bearer
//  secret is never here — see BackendConfig.swift.
//

import Combine
import Foundation
import SwiftUI

@MainActor
final class AppSettings: ObservableObject {
    private enum Key {
        static let baseURLOverride = "nad.baseURLOverride"
    }

    @AppStorage(Key.baseURLOverride) var baseURLOverrideString: String = ""

    /// The compiled-in default, shown in Settings for comparison against any override.
    var defaultBaseURL: URL { BackendConfig.baseURL }

    /// What NadTokenSource / BackendProbe should actually use.
    var effectiveBaseURL: URL {
        let trimmed = baseURLOverrideString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let url = URL(string: trimmed), url.scheme != nil, url.host != nil else {
            return defaultBaseURL
        }
        return url
    }

    var isUsingOverride: Bool {
        !baseURLOverrideString.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Set for exactly one connection when resuming a saved conversation. Resuming has
    /// to know the room name *before* asking for a token, because the transcript is
    /// parked under that name for the agent to collect — so for that one connect the
    /// client picks the name instead of the token server. Not persisted.
    @Published var oneShotRoomName: String?

    /// Only ever set for a resume. Otherwise nil, which lets the token server generate
    /// both the room (`nad-<hex8>`) and the participant identity (`user-<hex8>`) — see
    /// token_server.py. There's no reason to pin either by hand.
    var effectiveRoomName: String? {
        guard let oneShotRoomName, !oneShotRoomName.isEmpty else { return nil }
        return oneShotRoomName
    }

    func resetBaseURLOverride() {
        baseURLOverrideString = ""
    }
}
