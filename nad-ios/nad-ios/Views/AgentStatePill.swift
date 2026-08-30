//
//  AgentStatePill.swift
//  nad-ios
//

import LiveKit
import SwiftUI

struct AgentStatePill: View {
    /// nil = no agent connected yet (still joining, or the session hasn't started).
    var agentState: AgentState?
    var isPending: Bool

    private var label: String {
        guard let agentState else { return isPending ? "connecting" : "offline" }
        return agentState.rawValue
    }

    private var dotColor: SwiftUI.Color {
        switch agentState {
        case .listening, .thinking, .speaking:
            NadTheme.Color.ember
        case .idle, .initializing, nil:
            NadTheme.Color.mist
        }
    }

    private var isLive: Bool {
        switch agentState {
        case .listening, .thinking, .speaking: true
        default: false
        }
    }

    var body: some View {
        HStack(spacing: NadTheme.Space.xs) {
            Circle()
                .fill(dotColor)
                .frame(width: 6, height: 6)
                .shadow(color: isLive ? dotColor.opacity(0.8) : .clear, radius: 4)

            Text(label)
                .font(NadTheme.Typography.micro)
                .tracking(NadTheme.Typography.microTracking)
                .foregroundStyle(NadTheme.Color.bone)
        }
        .padding(.horizontal, NadTheme.Space.sm)
        .padding(.vertical, NadTheme.Space.xxs + 2)
        .background(
            Capsule()
                .fill(NadTheme.Color.ink)
                .overlay(Capsule().stroke(NadTheme.Color.graphite, lineWidth: 1))
        )
        .animation(NadTheme.Motion.state, value: label)
    }
}

#Preview {
    VStack(spacing: 12) {
        AgentStatePill(agentState: .speaking, isPending: false)
        AgentStatePill(agentState: .listening, isPending: false)
        AgentStatePill(agentState: .thinking, isPending: false)
        AgentStatePill(agentState: nil, isPending: true)
    }
    .padding()
    .background(NadTheme.Color.void)
}
