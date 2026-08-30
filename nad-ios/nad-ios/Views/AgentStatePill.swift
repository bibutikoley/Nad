//
//  AgentStatePill.swift
//  nad-ios
//
//  Reports the session phase, not the raw agent state. The distinction matters: the
//  raw state is nil for the whole cold start and again after a disconnect, and
//  rendering that as "offline" (or leaving the last live value on screen) tells the
//  user something untrue about whether the mic is open.
//

import LiveKit
import SwiftUI

struct AgentStatePill: View {
    var phase: VoiceSessionController.Phase
    /// Muting is a client-side act: the agent stays in `listening` on the wire because
    /// nothing tells it otherwise. So the pill has to be told separately, or it sits there
    /// claiming the agent is listening while the mic is shut -- the same class of untruth
    /// the phase enum above exists to prevent.
    var isMuted: Bool = false

    /// Only meaningful once the session is live; muting before that changes nothing the
    /// user can act on, and "muted" would mask the more useful "connecting".
    private var isMutedAndReady: Bool {
        if case .ready = phase { return isMuted }
        return false
    }

    private var label: String {
        if isMutedAndReady { return "muted" }
        switch phase {
        case .idle: return ""
        case .connecting: return "connecting"
        case .warmingUp: return "warming up"
        case let .ready(state): return state.rawValue
        case .failed: return "offline"
        }
    }

    private var dotColor: SwiftUI.Color {
        if isMutedAndReady { return NadTheme.Color.mist }
        switch phase {
        case .ready: return NadTheme.Color.ember
        case .failed: return NadTheme.Color.fault
        case .idle, .connecting, .warmingUp: return NadTheme.Color.mist
        }
    }

    /// Drives the ember tint on the capsule. Muted deliberately reads as not-live: the
    /// warm glow is what says "this thing can hear you".
    private var isLive: Bool {
        if case .ready = phase { return !isMuted }
        return false
    }

    /// Warm-up is the one state that can last a long time with nothing else moving on
    /// screen, so the dot pulses to show the app hasn't simply stalled.
    private var isPulsing: Bool {
        switch phase {
        case .connecting, .warmingUp: true
        default: false
        }
    }

    @State private var pulse = false

    var body: some View {
        HStack(spacing: NadTheme.Space.xs) {
            Circle()
                .fill(dotColor)
                .frame(width: 6, height: 6)
                .shadow(color: isLive ? dotColor.opacity(0.8) : .clear, radius: 4)
                .opacity(isPulsing && pulse ? 0.35 : 1)
                .animation(
                    isPulsing
                        ? .easeInOut(duration: 0.9).repeatForever(autoreverses: true)
                        : NadTheme.Motion.state,
                    value: pulse
                )

            Text(label)
                .font(NadTheme.Typography.micro)
                .tracking(NadTheme.Typography.microTracking)
                .foregroundStyle(NadTheme.Color.bone)
        }
        .padding(.horizontal, NadTheme.Space.sm)
        .padding(.vertical, NadTheme.Space.xxs + 2)
        .glassEffect(
            isLive ? .regular.tint(NadTheme.Color.ember.opacity(0.22)) : .regular,
            in: Capsule()
        )
        .animation(NadTheme.Motion.state, value: label)
        .onAppear { pulse = isPulsing }
        .onChange(of: isPulsing) { _, newValue in pulse = newValue }
    }
}

#Preview {
    VStack(spacing: 12) {
        AgentStatePill(phase: .connecting)
        AgentStatePill(phase: .warmingUp)
        AgentStatePill(phase: .ready(.listening))
        AgentStatePill(phase: .ready(.thinking))
        AgentStatePill(phase: .ready(.speaking))
        AgentStatePill(phase: .ready(.listening), isMuted: true)
        AgentStatePill(phase: .failed("nope"))
    }
    .padding()
    .background(NadTheme.Color.void)
}
