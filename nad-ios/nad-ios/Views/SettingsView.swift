//
//  SettingsView.swift
//  nad-ios
//
//  Deliberately not a stock Form/List — flat hairline rows (NadRow) instead of
//  grouped-inset cells, per the radius-as-information rule: system/data surfaces
//  stay flat. The bearer secret (TOKEN_SERVER_AUTH_TOKEN) is never editable or
//  shown here — only the server URL, which is what actually changes on a LAN IP
//  renewal. See BackendConfig.swift for why.
//

import Combine
import SwiftUI

struct SettingsView: View {
    @ObservedObject var settings: AppSettings
    @StateObject private var probe: AppSettingsProbeHolder
    @Environment(\.dismiss) private var dismiss
    @State private var urlOverrideDraft: String = ""
    @FocusState private var focusedField: Field?

    private enum Field { case url, room, identity }

    init(settings: AppSettings) {
        self.settings = settings
        _probe = StateObject(wrappedValue: AppSettingsProbeHolder(settings: settings))
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: NadTheme.Space.xl) {
                    section("Server") {
                        NadRow(showTopRule: false) {
                            VStack(alignment: .leading, spacing: NadTheme.Space.xxs) {
                                NadSectionLabel(text: "Token server URL")
                                TextField(settings.defaultBaseURL.absoluteString, text: $urlOverrideDraft)
                                    .font(NadTheme.Typography.data)
                                    .foregroundStyle(NadTheme.Color.bone)
                                    .tint(NadTheme.Color.ember)
                                    .keyboardType(.URL)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                                    .focused($focusedField, equals: .url)
                                    .onSubmit { commitURLOverride() }
                                Text(settings.isUsingOverride ? "Overriding the compiled-in default." : "Using the compiled-in default from BackendConfig.swift.")
                                    .font(NadTheme.Typography.data)
                                    .foregroundStyle(NadTheme.Color.mist)
                            }
                        }
                        if settings.isUsingOverride {
                            NadRow {
                                Button("Reset to default") {
                                    settings.resetBaseURLOverride()
                                    urlOverrideDraft = ""
                                }
                                .font(NadTheme.Typography.label)
                                .foregroundStyle(NadTheme.Color.ember)
                            }
                        }
                    }

                    section("Session") {
                        NadRow(showTopRule: false) {
                            labeledField(label: "Room name", placeholder: "auto-generated", text: $settings.roomName, field: .room)
                        }
                        NadRow {
                            labeledField(label: "Participant identity", placeholder: "auto-generated", text: $settings.participantIdentity, field: .identity)
                        }
                    }

                    section("Backend auth") {
                        NadRow(showTopRule: false) {
                            HStack {
                                NadSectionLabel(text: "Bearer token")
                                Spacer()
                                let configured = !BackendConfig.authToken.hasPrefix("REPLACE_WITH")
                                Text(configured ? "configured" : "missing")
                                    .font(NadTheme.Typography.data)
                                    .foregroundStyle(configured ? NadTheme.Color.ember : NadTheme.Color.fault)
                            }
                        }
                        Text("Set in BackendConfig.swift, not here — it's a build-time secret, not a per-device setting.")
                            .font(NadTheme.Typography.data)
                            .foregroundStyle(NadTheme.Color.mist)
                            .padding(.top, NadTheme.Space.xxs)
                    }

                    section("Connection test") {
                        VStack(alignment: .leading, spacing: NadTheme.Space.md) {
                            ForEach(probe.probe.steps) { step in
                                ConnectionTestRow(step: step)
                            }
                        }
                        .padding(.top, NadTheme.Space.sm)

                        Button {
                            commitURLOverride()
                            Task { await probe.probe.run() }
                        } label: {
                            HStack {
                                if probe.probe.isRunning {
                                    ProgressView().tint(NadTheme.Color.void)
                                }
                                Text(probe.probe.isRunning ? "Testing…" : "Run test")
                                    .font(NadTheme.Typography.label)
                                    .tracking(NadTheme.Typography.labelTracking)
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, NadTheme.Space.sm)
                            .foregroundStyle(NadTheme.Color.void)
                            .background(RoundedRectangle(cornerRadius: NadTheme.Radius.system).fill(NadTheme.Color.ember))
                        }
                        .disabled(probe.probe.isRunning)
                        .padding(.top, NadTheme.Space.lg)
                    }
                }
                .padding(NadTheme.Space.md)
            }
            .background(NadTheme.Color.void.ignoresSafeArea())
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(NadTheme.Color.void, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        commitURLOverride()
                        dismiss()
                    }
                    .foregroundStyle(NadTheme.Color.ember)
                }
            }
        }
        .preferredColorScheme(.dark)
        .onAppear { urlOverrideDraft = settings.baseURLOverrideString }
    }

    @ViewBuilder
    private func section(_ title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            NadSectionLabel(text: title)
                .padding(.bottom, NadTheme.Space.xs)
            content()
        }
    }

    private func labeledField(label: String, placeholder: String, text: Binding<String>, field: Field) -> some View {
        VStack(alignment: .leading, spacing: NadTheme.Space.xxs) {
            NadSectionLabel(text: label)
            TextField(placeholder, text: text)
                .font(NadTheme.Typography.data)
                .foregroundStyle(NadTheme.Color.bone)
                .tint(NadTheme.Color.ember)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .focused($focusedField, equals: field)
        }
    }

    private func commitURLOverride() {
        settings.baseURLOverrideString = urlOverrideDraft
    }
}

/// StateObject can't be initialized with a value that depends on another
/// property in the same init in a way SwiftUI likes for @ObservedObject sources,
/// so BackendProbe lives behind this tiny holder, constructed once in `init`.
@MainActor
final class AppSettingsProbeHolder: ObservableObject {
    let probe: BackendProbe
    init(settings: AppSettings) {
        probe = BackendProbe(settings: settings)
    }
}

#Preview {
    SettingsView(settings: AppSettings())
}
