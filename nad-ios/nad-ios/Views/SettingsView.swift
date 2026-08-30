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
    /// Observed directly. Held behind a wrapper object it would compile but never
    /// redraw — SwiftUI can't see a nested ObservableObject reached through a `let`,
    /// which is why the test's per-step indicators used to never appear.
    @StateObject private var probe: BackendProbe
    @Environment(\.dismiss) private var dismiss
    @State private var urlOverrideDraft: String = ""
    @FocusState private var urlFieldFocused: Bool

    init(settings: AppSettings) {
        self.settings = settings
        _probe = StateObject(wrappedValue: BackendProbe(settings: settings))
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: NadTheme.Space.xl) {
                    section("Server") {
                        NadRow(showTopRule: false) {
                            VStack(alignment: .leading, spacing: NadTheme.Space.xs) {
                                NadSectionLabel(text: "Token server URL")

                                // Prefilled with the URL actually in use, so the field
                                // shows the current value rather than hiding it behind
                                // a placeholder you have to clear to discover.
                                TextField(Self.urlHint, text: $urlOverrideDraft)
                                    .font(NadTheme.Typography.data)
                                    .foregroundStyle(NadTheme.Color.bone)
                                    .tint(NadTheme.Color.ember)
                                    .keyboardType(.URL)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                                    .focused($urlFieldFocused)
                                    .onSubmit { commitURLOverride() }
                                    .padding(.horizontal, NadTheme.Space.sm)
                                    .padding(.vertical, NadTheme.Space.xs)
                                    .glassEffect(
                                        urlFieldFocused
                                            ? .regular.tint(NadTheme.Color.ember.opacity(0.22))
                                            : .regular,
                                        in: RoundedRectangle(cornerRadius: NadTheme.Radius.system * 2, style: .continuous)
                                    )

                                VStack(alignment: .leading, spacing: 2) {
                                    // graphite is a divider tone — as text on `void`
                                    // it's effectively invisible.
                                    Text(Self.urlHint)
                                        .font(NadTheme.Typography.data)
                                        .foregroundStyle(NadTheme.Color.mist)
                                    Text(settings.isUsingOverride ? "Overriding the compiled-in default." : "Using the compiled-in default from BackendConfig.swift.")
                                        .font(NadTheme.Typography.data)
                                        .foregroundStyle(NadTheme.Color.mist.opacity(0.7))
                                }
                            }
                        }
                        if settings.isUsingOverride {
                            NadRow {
                                Button("Reset to default", role: .cancel) {
                                    settings.resetBaseURLOverride()
                                    urlOverrideDraft = settings.defaultBaseURL.absoluteString
                                }
                                .font(NadTheme.Typography.label)
                                .foregroundStyle(NadTheme.Color.ember)
                            }
                        }
                    }

                    // No bearer-token section: it's a build-time secret in
                    // BackendConfig.swift with nothing to configure here, and the
                    // connection test's "Bearer token accepted" step reports whether it
                    // actually works — which is the only thing worth knowing about it.
                    section("Connection test") {
                        VStack(alignment: .leading, spacing: NadTheme.Space.md) {
                            ForEach(probe.steps) { step in
                                ConnectionTestRow(step: step)
                            }
                        }
                        .padding(.top, NadTheme.Space.sm)

                        Button {
                            commitURLOverride()
                            Task { await probe.run() }
                        } label: {
                            HStack {
                                if probe.isRunning {
                                    ProgressView().controlSize(.small)
                                }
                                Text(probe.isRunning ? "Testing…" : "Run test")
                                    .font(NadTheme.Typography.label)
                                    .tracking(NadTheme.Typography.labelTracking)
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.glassProminent)
                        .tint(NadTheme.Color.ember)
                        .controlSize(.large)
                        .disabled(probe.isRunning)
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
        .onAppear { urlOverrideDraft = settings.effectiveBaseURL.absoluteString }
    }

    @ViewBuilder
    private func section(_ title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            NadSectionLabel(text: title)
                .padding(.bottom, NadTheme.Space.xs)
            content()
        }
    }

    /// Shown both as the placeholder (if the field is cleared) and as a standing hint,
    /// since the prefilled value means the placeholder alone would rarely be seen.
    private static let urlHint = "http://<IP_ADDRESS>:<PORT>"

    private func commitURLOverride() {
        let trimmed = urlOverrideDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        // The field starts prefilled with the URL in use, so submitting it untouched
        // must not pin an override — otherwise just opening Settings would freeze the
        // current LAN IP in place, which is the exact thing the override exists to fix.
        if trimmed.isEmpty || trimmed == settings.defaultBaseURL.absoluteString {
            settings.resetBaseURLOverride()
        } else {
            settings.baseURLOverrideString = trimmed
        }
    }
}

#Preview {
    SettingsView(settings: AppSettings())
}
