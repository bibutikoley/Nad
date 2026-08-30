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

    /// Once there's something to read, the blob gives up the stage and shrinks into the
    /// session controls at the bottom, between the mic and end buttons.
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
        .animation(NadTheme.Motion.state, value: hasTranscript)
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

            if controller.phase != .idle {
                AgentStatePill(phase: controller.phase, isMuted: controller.isMicMuted)
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

            VoiceVisualizer(level: 0.08)
                .frame(width: 240, height: 240)
                .opacity(isBusy ? 0.45 : 1)

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
                AudioReactiveBlob(
                    mic: controller.micLevelMonitor,
                    agent: controller.agentLevelMonitor,
                    phase: controller.phase,
                    isMuted: controller.isMicMuted
                )
                    .frame(width: 190, height: 190)
                    .matchedGeometryEffect(id: Self.blobID, in: blobSpace)
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
                // The stage blob's resting place once a transcript pushes it out of the
                // centre. Guarded on `hasTranscript` because the 190pt instance above is
                // what's on screen until then, and two views sharing a matchedGeometry
                // id at once trips an assertion.
                //
                // An overlay rather than a third element in the HStack. As a stack child
                // its size fed back into the row's sizing pass; an overlay is proposed
                // the row's size and can never influence it, so the blob is purely
                // drawn, never measured. It also keeps the row exactly as tall as the
                // buttons and lets the glow spill past them, which is what we wanted
                // anyway. Two frames: the inner one is the drawing surface, the outer
                // one the footprint the glow is allowed to overflow.
                .overlay {
                    if hasTranscript {
                        AudioReactiveBlob(
                            mic: controller.micLevelMonitor,
                            agent: controller.agentLevelMonitor,
                            phase: controller.phase,
                            isMuted: controller.isMicMuted
                        )
                        .frame(width: 124, height: 124)
                        .matchedGeometryEffect(id: Self.blobID, in: blobSpace)
                        .allowsHitTesting(false)
                    }
                }
            }
            .padding(NadTheme.Space.md)
        }
    }

    private static let blobID = "nad.blob"

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

    private var level: Float {
        if case .ready(.speaking) = phase {
            return agent.level
        }
        return isMuted ? 0 : mic.level
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
