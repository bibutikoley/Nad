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
    @StateObject private var controller: VoiceSessionController

    init() {
        let settings = AppSettings()
        _settings = StateObject(wrappedValue: settings)
        _controller = StateObject(wrappedValue: VoiceSessionController(settings: settings))
    }

    var body: some View {
        VoiceView(controller: controller, settings: settings)
    }
}

#Preview {
    ContentView()
}
