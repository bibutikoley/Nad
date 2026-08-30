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
        static let roomName = "nad.roomName"
        static let participantIdentity = "nad.participantIdentity"
    }

    @AppStorage(Key.baseURLOverride) var baseURLOverrideString: String = ""
    @AppStorage(Key.roomName) var roomName: String = ""
    @AppStorage(Key.participantIdentity) var participantIdentity: String = ""

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

    /// nil lets the token server generate one (see token_server.py: `nad-<hex8>`).
    var effectiveRoomName: String? {
        let trimmed = roomName.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// nil lets the token server generate one (`user-<hex8>`).
    var effectiveParticipantIdentity: String? {
        let trimmed = participantIdentity.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    func resetBaseURLOverride() {
        baseURLOverrideString = ""
    }
}
