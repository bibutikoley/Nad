//
//  Theme.swift
//  nad-ios
//
//  Design system for Nad. "Nad" (नाद) is Sanskrit for primordial sound / resonance —
//  the visual language leans into that rather than a generic "AI assistant" look:
//  warm amber-copper instead of the clichéd cool blue/violet, a phosphor-terminal
//  mood (self-hosted, analog, technical) instead of a glossy consumer-cloud one.
//
//  Radius is used as information: large soft radii mark conversational / human
//  surfaces (the visualizer, message bubbles); flat hairline-bordered rows mark
//  system / data surfaces (Settings, connection-test rows). Type is split the
//  same way — SF Pro for what was said, SF Mono for what the system is doing.
//

import SwiftUI

enum NadTheme {

    // MARK: - Color

    enum Color {
        /// Base background. Slightly blue-cool, never a flat pure black.
        static let void = SwiftUI.Color(hex: 0x0B0C0E)
        /// Elevated surface — cards, sheets, the composer bar.
        static let ink = SwiftUI.Color(hex: 0x16181C)
        /// Secondary elevated surface — nested rows on top of `ink`.
        static let slate = SwiftUI.Color(hex: 0x1D2024)
        /// Hairline dividers and unfilled strokes.
        static let graphite = SwiftUI.Color(hex: 0x2A2D32)
        /// Secondary / tertiary text, inactive labels.
        static let mist = SwiftUI.Color(hex: 0x8D9199)
        /// Primary text. Off-white, not #FFFFFF.
        static let bone = SwiftUI.Color(hex: 0xF0F1F3)
        /// The single accent: warm amber-copper. Reads as "signal" — phosphor,
        /// resonance, warmth — deliberately not the standard AI blue/violet/green.
        static let ember = SwiftUI.Color(hex: 0xFF8A42)
        /// Dimmer ember for backgrounds/fills behind ember content.
        static let emberDim = SwiftUI.Color(hex: 0xFF8A42).opacity(0.16)
        /// The one semantic exception to "single accent": failure states only.
        static let fault = SwiftUI.Color(hex: 0xE5484D)
    }

    // MARK: - Type

    /// SF Pro for anything a human or the agent said — conversational content.
    /// SF Mono for anything the system is reporting — state, metrics, config.
    enum Typography {
        static let display = Font.system(size: 34, weight: .bold, design: .default)
        static let title = Font.system(size: 22, weight: .semibold, design: .default)
        static let body = Font.system(size: 17, weight: .regular, design: .default)
        static let bodyEmphasis = Font.system(size: 17, weight: .medium, design: .default)

        /// Uppercase status/section labels: "LISTENING", "ROOM", "CONNECTION TEST".
        static let label = Font.system(size: 13, weight: .medium, design: .monospaced)
        /// Timestamps, latency numbers, IPs — tertiary technical metadata.
        static let data = Font.system(size: 12, weight: .regular, design: .monospaced)
        /// Pill text, micro badges.
        static let micro = Font.system(size: 11, weight: .semibold, design: .monospaced)

        static let labelTracking: CGFloat = 1.1
        static let microTracking: CGFloat = 0.9
    }

    // MARK: - Spacing

    /// 4pt base grid.
    enum Space {
        static let xxs: CGFloat = 4
        static let xs: CGFloat = 8
        static let sm: CGFloat = 12
        static let md: CGFloat = 16
        static let lg: CGFloat = 24
        static let xl: CGFloat = 32
        static let xxl: CGFloat = 48
        static let xxxl: CGFloat = 64
    }

    // MARK: - Radius

    enum Radius {
        /// Conversational / human surfaces — the visualizer stage, message bubbles.
        static let conversational: CGFloat = 28
        /// System / data surfaces — settings rows, config panels.
        static let system: CGFloat = 4
        static let pill: CGFloat = 999
    }

    // MARK: - Motion

    enum Motion {
        /// State transitions: agent state pill, connect/disconnect, screen changes.
        static let state = Animation.spring(response: 0.4, dampingFraction: 0.75)
        /// Fast micro-reactions: visualizer responding to a fresh audio sample.
        static let reaction = Animation.spring(response: 0.25, dampingFraction: 0.6)
        /// Slow ambient idle breathing loop.
        static let breathe = Animation.easeInOut(duration: 4).repeatForever(autoreverses: true)
        /// One outward ripple on speech onset — literally "nad": a sound propagating.
        static let ripple = Animation.easeOut(duration: 1.1)
    }
}

extension SwiftUI.Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: opacity
        )
    }
}

// MARK: - Shared row chrome (Settings etc. — no stock List/Form styling)

/// A flat, hairline-bordered row used instead of stock List/Form cells.
/// Radius = `.system` (near-flat) per the radius-as-information rule above.
struct NadRow<Content: View>: View {
    var showTopRule = true
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) {
            if showTopRule {
                Rectangle()
                    .fill(NadTheme.Color.graphite)
                    .frame(height: 1)
            }
            content
                .padding(.vertical, NadTheme.Space.sm)
        }
    }
}

struct NadSectionLabel: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(NadTheme.Typography.label)
            .tracking(NadTheme.Typography.labelTracking)
            .foregroundStyle(NadTheme.Color.mist)
    }
}
