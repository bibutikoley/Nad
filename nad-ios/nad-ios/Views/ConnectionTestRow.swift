//
//  ConnectionTestRow.swift
//  nad-ios
//
//  One row per BackendProbe.ProbeStep. The three rows read top-to-bottom as the
//  actual signal chain from nad-backend/README.md: token server → bearer auth →
//  LiveKit — so a failure's position tells you which hop broke.
//

import SwiftUI

struct ConnectionTestRow: View {
    let step: ProbeStep

    var body: some View {
        HStack(alignment: .top, spacing: NadTheme.Space.sm) {
            icon
                .frame(width: 16, height: 16)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 2) {
                Text(step.title)
                    .font(NadTheme.Typography.bodyEmphasis)
                    .foregroundStyle(NadTheme.Color.bone)

                switch step.status {
                case .pending:
                    EmptyView()
                case .running:
                    Text("checking…")
                        .font(NadTheme.Typography.data)
                        .foregroundStyle(NadTheme.Color.mist)
                case let .success(detail):
                    Text(detail)
                        .font(NadTheme.Typography.data)
                        .foregroundStyle(NadTheme.Color.mist)
                case let .failure(message):
                    Text(message)
                        .font(NadTheme.Typography.data)
                        .foregroundStyle(NadTheme.Color.fault)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .animation(NadTheme.Motion.state, value: step.status)
    }

    @ViewBuilder
    private var icon: some View {
        switch step.status {
        case .pending:
            Circle().strokeBorder(NadTheme.Color.graphite, lineWidth: 1.5)
        case .running:
            ProgressView()
                .progressViewStyle(.circular)
                .tint(NadTheme.Color.ember)
                .scaleEffect(0.65)
        case .success:
            Circle().fill(NadTheme.Color.ember)
        case .failure:
            Circle().strokeBorder(NadTheme.Color.fault, lineWidth: 1.5)
        }
    }
}
