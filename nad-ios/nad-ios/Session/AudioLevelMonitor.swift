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

import Accelerate
import AVFoundation
import Combine
import LiveKit

final class AudioLevelMonitor: NSObject, AudioRenderer, ObservableObject, @unchecked Sendable {
    @Published private(set) var level: Float = 0

    /// The continuously-integrated value behind `level`.
    ///
    /// Kept separate so the smoothing can keep converging while `level` is only published
    /// when it moves visibly. Folding the threshold into `level` itself would stall it: the
    /// steps shrink as it approaches the target, so it would stop updating a little short
    /// of rest and the blob would never settle.
    private var raw: Float = 0

    /// Higher = faster rise/fall. Asymmetric so the level leads a rising voice and trails a
    /// falling one, rather than chattering around every syllable.
    ///
    /// Deliberately slow. Buffers arrive roughly every 10 ms, so an attack of 0.6 tracked
    /// the waveform almost instantaneously and the visualizer read as jittery rather than
    /// alive — it was following the envelope of individual syllables. These values settle
    /// over a few hundred milliseconds, which is the timescale a viewer perceives as
    /// "someone is talking" instead of "this thing is vibrating".
    private let attack: Float = 0.12
    private let release: Float = 0.04

    func render(pcmBuffer: AVAudioPCMBuffer) {
        let rms = Self.rms(of: pcmBuffer)
        Task { @MainActor [weak self] in
            self?.update(with: rms)
        }
    }

    @MainActor
    private func update(with rms: Float) {
        let coefficient = rms > raw ? attack : release
        raw += (rms - raw) * coefficient

        // `@Published` fires on every assignment, and buffers arrive ~100 times a second
        // for each of the two monitors. Publishing all of them invalidates every view
        // observing the controller — 200 SwiftUI transactions a second, each re-running the
        // whole layout. The visualizer redraws from its own TimelineView regardless, so it
        // only needs the value to be roughly current, not sample-accurate.
        guard abs(raw - level) >= 0.004 else { return }
        level = raw
    }

    /// Drops the level to silence immediately. Renderers stop firing when a session
    /// ends, so without this the last value before the disconnect would stay published
    /// and keep driving the visualizer.
    @MainActor
    func reset() {
        raw = 0
        level = 0
    }

    /// Reads whichever sample format the buffer actually carries.
    ///
    /// This has to handle Int16 first, not as a fallback: LiveKit builds these buffers with
    /// `AVAudioFormat(commonFormat: .pcmFormatInt16, ...)` before handing them to
    /// `AudioRenderer` (see `LKAudioBuffer.toAVAudioPCMBuffer()` in the SDK). So
    /// `floatChannelData` is *always* nil here, and reading only that returned 0 for every
    /// buffer ever delivered — the visualizer sat at a dead level and never moved.
    ///
    /// Uses vDSP because this runs on the audio thread, once per ~10 ms buffer.
    private static func rms(of buffer: AVAudioPCMBuffer) -> Float {
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else { return 0 }

        var rms: Float = 0
        if let samples = buffer.int16ChannelData?[0] {
            var floats = [Float](repeating: 0, count: frameCount)
            vDSP_vflt16(samples, 1, &floats, 1, vDSP_Length(frameCount))
            var fullScale = Float(Int16.max)
            vDSP_vsdiv(floats, 1, &fullScale, &floats, 1, vDSP_Length(frameCount))
            vDSP_rmsqv(floats, 1, &rms, vDSP_Length(frameCount))
        } else if let samples = buffer.floatChannelData?[0] {
            vDSP_rmsqv(samples, 1, &rms, vDSP_Length(frameCount))
        } else {
            return 0
        }

        // Perceptual-ish scaling: raw RMS for speech sits low (~0.02-0.2); a small
        // multiplier keeps normal speech in a usable visual range without clipping.
        return min(rms * 4, 1)
    }
}
