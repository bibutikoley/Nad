//
//  AudioLevelMonitor.swift
//  nad-ios
//
//  Turns raw PCM buffers into a smoothed 0...1 level for the visualizer. Used for
//  both directions: AudioManager.shared.add(localAudioRenderer:) for the mic, and
//  RemoteAudioTrack.add(audioRenderer:) on session.agent.audioTrack for the agent's
//  voice. AudioRenderer's render(pcmBuffer:) fires off the audio thread, so this
//  hops to the main actor before touching @Published state.
//

import AVFoundation
import Combine
import LiveKit

final class AudioLevelMonitor: NSObject, AudioRenderer, @unchecked Sendable {
    @Published private(set) var level: Float = 0

    /// Higher = faster rise/fall. Asymmetric attack/release makes the visualizer
    /// snap toward loud audio but settle gently, rather than jittering.
    private let attack: Float = 0.6
    private let release: Float = 0.15

    func render(pcmBuffer: AVAudioPCMBuffer) {
        let rms = Self.rms(of: pcmBuffer)
        Task { @MainActor [weak self] in
            self?.update(with: rms)
        }
    }

    @MainActor
    private func update(with rms: Float) {
        let coefficient = rms > level ? attack : release
        level += (rms - level) * coefficient
    }

    private static func rms(of buffer: AVAudioPCMBuffer) -> Float {
        guard let channelData = buffer.floatChannelData else { return 0 }
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else { return 0 }

        let samples = channelData[0]
        var sumOfSquares: Float = 0
        for i in 0 ..< frameCount {
            let sample = samples[i]
            sumOfSquares += sample * sample
        }
        let rms = sqrt(sumOfSquares / Float(frameCount))
        // Perceptual-ish scaling: raw RMS for speech sits low (~0.02-0.2); a small
        // multiplier keeps normal speech in a usable visual range without clipping.
        return min(rms * 4, 1)
    }
}
