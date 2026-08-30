//
//  ConnectionTestRow.swift
//  nad-ios
//
//  One row per BackendProbe.ProbeStep. The three rows read top-to-bottom as the
//  actual signal chain from nad-backend/README.md: token server → bearer auth →
//  LiveKit — so a failure's position tells you which hop broke.
//
//  Each status has its own motion, because on a LAN the checks are near-instant and a
//  static icon swap is easy to miss entirely: running sweeps and pulses, a result
//  springs in over it.
//

import SwiftUI

struct ConnectionTestRow: View {
    let step: ProbeStep

    var body: some View {
        HStack(alignment: .top, spacing: NadTheme.Space.sm) {
            icon
                .frame(width: 18, height: 18)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 2) {
                Text(step.title)
                    .font(NadTheme.Typography.bodyEmphasis)
                    .foregroundStyle(isPending ? NadTheme.Color.mist : NadTheme.Color.bone)

                switch step.status {
                case .pending:
                    EmptyView()
                case .running:
                    Text("checking…")
                        .font(NadTheme.Typography.data)
                        .foregroundStyle(NadTheme.Color.ember)
                        .transition(.opacity)
                case let .success(detail):
                    Text(detail)
                        .font(NadTheme.Typography.data)
                        .foregroundStyle(NadTheme.Color.mist)
                        .transition(.opacity)
                case let .failure(message):
                    Text(message)
                        .font(NadTheme.Typography.data)
                        .foregroundStyle(NadTheme.Color.fault)
                        .fixedSize(horizontal: false, vertical: true)
                        .transition(.opacity)
                }
            }
            Spacer(minLength: 0)
        }
        .animation(NadTheme.Motion.state, value: step.status)
    }

    private var isPending: Bool { step.status == .pending }

    @ViewBuilder
    private var icon: some View {
        switch step.status {
        case .pending:
            Circle()
                .strokeBorder(NadTheme.Color.graphite, lineWidth: 1.5)
        case .running:
            RunningIndicator()
        case .success:
            Image(systemName: "checkmark")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(NadTheme.Color.void)
                .frame(width: 18, height: 18)
                .background(Circle().fill(NadTheme.Color.ember))
                .transition(.scale(scale: 0.4).combined(with: .opacity))
        case .failure:
            Image(systemName: "xmark")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(NadTheme.Color.bone)
                .frame(width: 18, height: 18)
                .background(Circle().fill(NadTheme.Color.fault))
                .transition(.scale(scale: 0.4).combined(with: .opacity))
        }
    }
}

/// A sweeping arc over a pulsing ring — reads as "working" even for the ~10ms a LAN
/// check actually takes.
private struct RunningIndicator: View {
    @State private var sweep = false
    @State private var pulse = false

    var body: some View {
        ZStack {
            Circle()
                .stroke(NadTheme.Color.ember.opacity(0.22), lineWidth: 2)
                .scaleEffect(pulse ? 1.15 : 0.85)
                .opacity(pulse ? 0.3 : 0.9)

            Circle()
                .trim(from: 0, to: 0.72)
                .stroke(
                    NadTheme.Color.ember,
                    style: StrokeStyle(lineWidth: 2, lineCap: .round)
                )
                .rotationEffect(.degrees(sweep ? 360 : 0))
        }
        .onAppear {
            withAnimation(.linear(duration: 0.75).repeatForever(autoreverses: false)) {
                sweep = true
            }
            withAnimation(.easeInOut(duration: 0.55).repeatForever(autoreverses: true)) {
                pulse = true
            }
        }
    }
}

#Preview {
    VStack(alignment: .leading, spacing: NadTheme.Space.md) {
        ConnectionTestRow(step: ProbeStep(id: "a", title: "Token server reachable", status: .pending))
        ConnectionTestRow(step: ProbeStep(id: "b", title: "Bearer token accepted", status: .running))
        ConnectionTestRow(step: ProbeStep(id: "c", title: "LiveKit reachable", status: .success(detail: "192.168.0.228 · 6ms")))
        ConnectionTestRow(step: ProbeStep(id: "d", title: "LiveKit reachable", status: .failure(message: "Couldn't reach the token server.")))
    }
    .padding()
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(NadTheme.Color.void)
}
