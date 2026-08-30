//
//  ContentView.swift
//  nad-ios
//
//  Composition root: owns the one AppSettings instance and the one
//  VoiceSessionController built from it, so Settings edits and the session's
//  token source always agree.
//

import SwiftUI

struct ContentView: View {
    @StateObject private var settings: AppSettings
    @StateObject private var store: ConversationStore
    @StateObject private var controller: VoiceSessionController
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let settings = AppSettings()
        let store = ConversationStore()
        _settings = StateObject(wrappedValue: settings)
        _store = StateObject(wrappedValue: store)
        _controller = StateObject(wrappedValue: VoiceSessionController(settings: settings, store: store))
    }

    var body: some View {
        VoiceView(controller: controller, settings: settings, store: store)
            .onChange(of: scenePhase) { _, newValue in
                // Transcript writes are debounced, so force one out before the app can
                // be suspended or killed in the background.
                if newValue != .active {
                    Task { await store.flush() }
                }
            }
    }
}

#Preview {
    ContentView()
}
