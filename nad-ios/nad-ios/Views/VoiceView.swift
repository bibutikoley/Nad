//
//  VoiceView.swift
//  nad-ios
//
//  The main screen: idle (not yet connected) and active (in a session) states,
//  no stock List/Form chrome anywhere on it. `Session` is @MainActor +
//  ObservableObject, so `controller.session` is observed directly.
//

import LiveKit
import SwiftUI

struct VoiceView: View {
    @ObservedObject var controller: VoiceSessionController
    /// The same AppSettings instance the controller was built with — passed in
    /// separately (rather than constructed here) so Settings edits and the
    /// controller's token source share one instance.
    let settings: AppSettings

    @State private var showSettings = false
    @State private var previousLiveState: AgentState?
    @State private var rippleTrigger = 0

    private var session: Session { controller.session }

    var body: some View {
        ZStack {
            NadTheme.Color.void.ignoresSafeArea()

            VStack(spacing: 0) {
                header

                if session.isConnected {
                    activeSession
                } else {
                    idle
                }
            }
        }
        .sheet(isPresented: $showSettings) {
            SettingsView(settings: settings)
        }
        .onChange(of: session.agent.agentState) { _, newValue in
            guard isLive(newValue), newValue != previousLiveState else {
                previousLiveState = isLive(newValue) ? newValue : nil
                return
            }
            previousLiveState = newValue
            rippleTrigger += 1
        }
        .preferredColorScheme(.dark)
    }

    // MARK: - Header

    private var header: some View {
        HStack {
            Text("NAD")
                .font(NadTheme.Typography.label)
                .tracking(NadTheme.Typography.labelTracking * 2)
                .foregroundStyle(NadTheme.Color.mist)

            Spacer()

            AgentStatePill(agentState: session.agent.agentState, isPending: session.agent.isPending)

            Spacer()

            Button {
                showSettings = true
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(NadTheme.Color.mist)
            }
        }
        .padding(.horizontal, NadTheme.Space.md)
        .padding(.top, NadTheme.Space.sm)
        .padding(.bottom, NadTheme.Space.xs)
    }

    // MARK: - Idle

    private var idle: some View {
        VStack(spacing: NadTheme.Space.xl) {
            Spacer()

            VoiceVisualizer(level: 0.08, rippleTrigger: 0)
                .frame(width: 200, height: 200)

            VStack(spacing: NadTheme.Space.xs) {
                Text("Nad")
                    .font(NadTheme.Typography.display)
                    .foregroundStyle(NadTheme.Color.bone)
                Text("Your self-hosted voice agent, on your network.")
                    .font(NadTheme.Typography.body)
                    .foregroundStyle(NadTheme.Color.mist)
                    .multilineTextAlignment(.center)
            }

            if controller.micPermissionDenied {
                permissionBanner
            }

            if let error = session.error {
                errorBanner(error.localizedDescription) { session.dismissError() }
            }

            Spacer()

            Button {
                Task { await controller.start() }
            } label: {
                Text("Connect")
                    .font(NadTheme.Typography.bodyEmphasis)
                    .foregroundStyle(NadTheme.Color.void)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, NadTheme.Space.md)
                    .background(RoundedRectangle(cornerRadius: NadTheme.Radius.conversational, style: .continuous).fill(NadTheme.Color.ember))
            }
            .padding(.horizontal, NadTheme.Space.xl)
            .padding(.bottom, NadTheme.Space.xl)
        }
    }

    // MARK: - Active

    private var activeSession: some View {
        VStack(spacing: 0) {
            VoiceVisualizer(level: visualizerLevel, rippleTrigger: rippleTrigger)
                .frame(width: 160, height: 160)
                .padding(.top, NadTheme.Space.lg)
                .padding(.bottom, NadTheme.Space.md)

            if let agentError = session.agent.error {
                errorBanner(agentErrorMessage(agentError)) { }
                    .padding(.horizontal, NadTheme.Space.md)
            }

            TranscriptView(messages: session.messages)

            VStack(spacing: NadTheme.Space.sm) {
                ComposerView { text in
                    await controller.sendText(text)
                }

                HStack(spacing: NadTheme.Space.lg) {
                    roundButton(
                        systemImage: controller.isMicMuted ? "mic.slash.fill" : "mic.fill",
                        tint: controller.isMicMuted ? NadTheme.Color.fault : NadTheme.Color.bone
                    ) {
                        Task { await controller.toggleMicrophone() }
                    }

                    Spacer()

                    roundButton(systemImage: "xmark", tint: NadTheme.Color.void, fill: NadTheme.Color.fault) {
                        Task { await controller.end() }
                    }
                }
            }
            .padding(NadTheme.Space.md)
        }
    }

    private var visualizerLevel: Float {
        session.agent.agentState == .speaking ? controller.agentLevel : controller.micLevel
    }

    private func isLive(_ state: AgentState?) -> Bool {
        switch state {
        case .listening, .thinking, .speaking: true
        default: false
        }
    }

    private func agentErrorMessage(_ error: Agent.Error) -> String {
        switch error {
        case .timeout: "No agent joined. Is scripts/dev.sh running on the backend Mac?"
        case .left: "The agent left the room unexpectedly."
        }
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
        }
        .padding(NadTheme.Space.sm)
        .background(RoundedRectangle(cornerRadius: NadTheme.Radius.system).fill(NadTheme.Color.fault.opacity(0.12)))
        .padding(.horizontal, NadTheme.Space.xl)
    }

    private func roundButton(systemImage: String, tint: SwiftUI.Color, fill: SwiftUI.Color = NadTheme.Color.ink, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(tint)
                .frame(width: 52, height: 52)
                .background(Circle().fill(fill))
        }
        .animation(NadTheme.Motion.reaction, value: controller.isMicMuted)
    }
}

#Preview {
    VoiceView(controller: VoiceSessionController(settings: AppSettings()), settings: AppSettings())
}
