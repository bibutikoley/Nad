//
//  nad_iosApp.swift
//  nad-ios
//
//  Created by Bibuti Koley on 30/08/26.
//

import LiveKit
import SwiftUI

@main
struct nad_iosApp: App {
    init() {
        // Recommended by LiveKit: warm up the audio device module before any
        // AVAudioSession category changes, to avoid a startup audio glitch.
        AudioManager.prepare()
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
