//
//  VoiceView.swift
//  nad-ios
//
//  The main screen: idle (including connecting / warming up) and active (the agent is
//  genuinely ready) states, no stock List/Form chrome anywhere on it.
//
//  Everything state-ish comes from `controller`. Reading `controller.session` here
//  would compile but not observe — see the note at the top of VoiceSessionController.
//

import LiveKit
import SwiftUI

struct VoiceView: View {
    @ObservedObject var controller: VoiceSessionController
    /// The same AppSettings instance the controller was built with — passed in
    /// separately (rather than constructed here) so Settings edits and the
    /// controller's token source share one instance.
    let settings: AppSettings
    @ObservedObject var store: ConversationStore

    @State private var showSettings = false
    @State private var showHistory = false
    @Namespace private var blobSpace

    /// Once there's something to read, the blob gives up the stage and settles into the
    /// header, beside the pill that names the same state in words.
    private var hasTranscript: Bool { !controller.messages.isEmpty }

    var body: some View {
        ZStack {
            NadTheme.Color.void.ignoresSafeArea()

            VStack(spacing: 0) {
                header

                if controller.phase.isReady {
                    activeSession
                } else {
                    idle
                }
            }

            // The one and only blob. Every position in this file reserves space with a
            // `blobAnchor` and draws nothing; this instance does all the drawing, taking its
            // frame from whichever anchor is currently the source.
            //
            // One instance rather than one per position: it genuinely travels between the
            // three places instead of crossfading between look-alikes, there is no way for two
            // views to hold the shared matchedGeometry id at once, and the audio-level
            // subscriptions inside it are made once for the life of the screen rather than torn
            // down and rebuilt on every transition.
            AudioReactiveBlob(
                mic: controller.micLevelMonitor,
                agent: controller.agentLevelMonitor,
                phase: controller.phase,
                isMuted: controller.isMicMuted
            )
            .matchedGeometryEffect(id: Self.blobID, in: blobSpace, isSource: false)
            .opacity(isBusy ? 0.45 : 1)
            // It floats over the header buttons on its way past; it must never eat their taps.
            .allowsHitTesting(false)
        }
        .sheet(isPresented: $showSettings) {
            SettingsView(settings: settings)
        }
        .sheet(isPresented: $showHistory) {
            HistoryView(store: store) { conversation in
                Task {
                    // Resuming replaces whatever is on screen, so end the current
                    // session first rather than connecting on top of it.
                    if controller.phase != .idle { await controller.end() }
                    await controller.start(resuming: conversation)
                }
            }
        }
        // The blob's two journeys: landing -> stage when the session goes live, stage -> header
        // when the first line of transcript arrives. Both have to be animated here, on the
        // common ancestor of the anchors involved, or the blob teleports.
        .animation(NadTheme.Motion.state, value: hasTranscript)
        .animation(NadTheme.Motion.state, value: controller.phase.isReady)
        .preferredColorScheme(.dark)
    }

    // MARK: - Header

    private var header: some View {
        HStack {
            Button {
                showHistory = true
            } label: {
                Image(systemName: "clock.arrow.circlepath")
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(NadTheme.Color.mist)
            }
            .buttonStyle(.glass)
            .accessibilityLabel("Conversation history")

            Spacer()

            // Blob first, then pill: the signal, then the word for it. The blob only docks here
            // once there's a transcript to sit above — before that it has the stage below.
            HStack(spacing: NadTheme.Space.xs) {
                if controller.phase.isReady, hasTranscript {
                    blobAnchor(geometry: 50, footprint: 26)
                }

                if controller.phase != .idle {
                    AgentStatePill(phase: controller.phase, isMuted: controller.isMicMuted)
                }
            }

            Spacer()

            Button {
                showSettings = true
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(NadTheme.Color.mist)
            }
            .buttonStyle(.glass)
            .accessibilityLabel("Settings")
        }
        .padding(.horizontal, NadTheme.Space.md)
        .padding(.top, NadTheme.Space.sm)
        .padding(.bottom, NadTheme.Space.xs)
    }

    // MARK: - Idle

    private var idle: some View {
        VStack(spacing: NadTheme.Space.xl) {
            Spacer()

            blobAnchor(geometry: 240)

            VStack(spacing: NadTheme.Space.xs) {
                Text("Nad")
                    .font(NadTheme.Typography.display)
                    .foregroundStyle(NadTheme.Color.bone)
                Text(subtitle)
                    .font(NadTheme.Typography.body)
                    .foregroundStyle(NadTheme.Color.mist)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, NadTheme.Space.lg)
            }

            if controller.micPermissionDenied {
                permissionBanner
            }

            if case let .failed(message) = controller.phase {
                errorBanner(message) { controller.dismissError() }
            }

            Spacer()

            connectButton
                .padding(.horizontal, NadTheme.Space.xl)
                .padding(.bottom, NadTheme.Space.xl)
        }
    }

    private var subtitle: String {
        switch controller.phase {
        case .connecting:
            "Reaching the agent on your network…"
        case .warmingUp:
            "Loading speech models — the first run downloads them, so this can take a while."
        default:
            "Your self-hosted voice agent, on your network."
        }
    }

    /// Connecting and warming up both mean "not ready" — the button must not invite a
    /// second tap, and the mic stays closed throughout.
    private var isBusy: Bool {
        switch controller.phase {
        case .connecting, .warmingUp: true
        default: false
        }
    }

    private var connectButton: some View {
        Button {
            Task { await controller.start() }
        } label: {
            HStack(spacing: NadTheme.Space.xs) {
                if isBusy {
                    ProgressView()
                        .controlSize(.small)
                        .tint(NadTheme.Color.mist)
                }
                Text(connectLabel)
                    .font(NadTheme.Typography.bodyEmphasis)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, NadTheme.Space.xs)
        }
        .buttonStyle(.glassProminent)
        .tint(isBusy ? NadTheme.Color.slate : NadTheme.Color.ember)
        .controlSize(.large)
        .disabled(isBusy)
        .animation(NadTheme.Motion.state, value: isBusy)
    }

    private var connectLabel: String {
        switch controller.phase {
        case .connecting: "Connecting…"
        case .warmingUp: "Warming up…"
        case .failed: "Try again"
        default: "Connect"
        }
    }

    // MARK: - Active

    private var activeSession: some View {
        VStack(spacing: 0) {
            if !hasTranscript {
                blobAnchor(geometry: 190)
                    .padding(.top, NadTheme.Space.lg)
                    .padding(.bottom, NadTheme.Space.md)
            }

            TranscriptView(messages: controller.messages)

            // Voice is the only way in. There is no text composer: an input field sharing
            // the bar with the mic asked the user to talk and to type in the same breath,
            // which read as neither being the real invitation.
            GlassEffectContainer(spacing: NadTheme.Space.lg) {
                HStack(spacing: NadTheme.Space.lg) {
                    // Muted is the state worth seeing at a glance, so it takes the
                    // tinted-prominent treatment and live mic stays plain glass.
                    Button {
                        Task { await controller.toggleMicrophone() }
                    } label: {
                        Image(systemName: controller.isMicMuted ? "mic.slash.fill" : "mic.fill")
                            .font(.system(size: 18, weight: .medium))
                            .frame(width: 32, height: 32)
                    }
                    .buttonStyle(.glass)
                    .tint(controller.isMicMuted ? NadTheme.Color.fault : NadTheme.Color.bone)
                    .controlSize(.large)
                    .accessibilityLabel(controller.isMicMuted ? "Unmute microphone" : "Mute microphone")

                    Spacer()

                    Button {
                        Task { await controller.end() }
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 18, weight: .medium))
                            .frame(width: 32, height: 32)
                    }
                    .buttonStyle(.glassProminent)
                    .tint(NadTheme.Color.fault)
                    .controlSize(.large)
                    .accessibilityLabel("End session")
                }
                .animation(NadTheme.Motion.reaction, value: controller.isMicMuted)
            }
            .padding(NadTheme.Space.md)
        }
    }

    // MARK: - Blob placement

    private static let blobID = "nad.blob"

    /// Reserves the blob's place without drawing anything — the drawing is done once, by the
    /// instance in `body`.
    ///
    /// `geometry` is the frame the blob takes on while this anchor is the live one.
    /// `footprint` is what the surrounding layout actually reserves, and defaults to the same
    /// thing. They differ only in the header, where a 50pt blob has to sit in a row no taller
    /// than its buttons: the inner frame is the drawing surface the geometry effect measures,
    /// the outer one the smaller footprint the glow is allowed to overflow.
    ///
    /// Exactly one anchor may be a source at any moment — two holding the id at once trips an
    /// assertion, and none at all collapses the blob to nothing — so the conditions guarding
    /// the three call sites have to stay a strict partition.
    private func blobAnchor(geometry: CGFloat, footprint: CGFloat? = nil) -> some View {
        Color.clear
            .frame(width: geometry, height: geometry)
            .matchedGeometryEffect(id: Self.blobID, in: blobSpace, isSource: true)
            .frame(width: footprint ?? geometry, height: footprint ?? geometry)
    }

    // MARK: - Shared bits

    private var permissionBanner: some View {
        Button {
            if let url = URL(string: UIApplication.openSettingsURLString) {
                UIApplication.shared.open(url)
            }
        } label: {
            errorBanner("Microphone access is off. Tap to open Settings.") { }
        }
        .padding(.horizontal, NadTheme.Space.md)
    }

    private func errorBanner(_ message: String, onDismiss: @escaping () -> Void) -> some View {
        HStack(alignment: .top, spacing: NadTheme.Space.sm) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(NadTheme.Color.fault)
            Text(message)
                .font(NadTheme.Typography.data)
                .foregroundStyle(NadTheme.Color.bone)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(NadTheme.Color.mist)
            }
        }
        .padding(NadTheme.Space.sm)
        .glassEffect(
            .regular.tint(NadTheme.Color.fault.opacity(0.28)),
            in: RoundedRectangle(cornerRadius: NadTheme.Radius.conversational, style: .continuous)
        )
        .padding(.horizontal, NadTheme.Space.xl)
    }

}

/// Owns the audio-level subscription so per-buffer level updates invalidate only the
/// blob. Reading the levels straight from `VoiceView` would re-render the header,
/// transcript and composer at audio-buffer rate.
private struct AudioReactiveBlob: View {
    /// Subscribed to directly, not read off the controller. The controller is observed by
    /// `VoiceView` itself, so routing audio levels through it invalidated the entire screen
    /// — transcript included — on every audio buffer. Observing the monitors here keeps
    /// that firehose contained to this view.
    @ObservedObject var mic: AudioLevelMonitor
    @ObservedObject var agent: AudioLevelMonitor
    /// Plain values, not the controller: these change a handful of times per session, and
    /// taking the whole object back would re-open the same invalidation path.
    var phase: VoiceSessionController.Phase
    var isMuted: Bool
    var radiusRatio: CGFloat = 0.24

    /// What the blob shows when there is no session to react to. The landing page used to
    /// hardcode this on a visualizer of its own; now that one instance covers every state, the
    /// resting breath lives here. Not zero: an entirely still blob reads as broken.
    private static let resting: Float = 0.08

    private var level: Float {
        switch phase {
        case .ready(.speaking): agent.level
        case .ready: isMuted ? 0 : mic.level
        default: Self.resting
        }
    }

    var body: some View {
        VoiceVisualizer(level: level, radiusRatio: radiusRatio)
    }
}

#Preview {
    let settings = AppSettings()
    let store = ConversationStore(filename: "preview.json")
    return VoiceView(
        controller: VoiceSessionController(settings: settings, store: store),
        settings: settings,
        store: store
    )
}
