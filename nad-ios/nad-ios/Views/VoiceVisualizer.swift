//
//  VoiceVisualizer.swift
//  nad-ios
//
//  The signature element. "Nad" (नाद) — sound, resonance — rendered as one
//  continuous organic form whose edge wobbles with live audio level, not a row
//  of EQ bars. On each speech onset a thin ring ripples outward and fades: sound
//  literally propagating, which is the one visual idea this whole app is built
//  around.
//
//  Every dimension is derived from the frame it's given, so the same view is both the
//  large stage blob and the small header icon it collapses into.
//

import SwiftUI

struct VoiceVisualizer: View {
    /// Combined, smoothed 0...1 audio level (mic while listening, agent while speaking).
    var level: Float
    /// Bump this to fire one outward ripple — call on each speech-onset edge.
    var rippleTrigger: Int
    var accent: SwiftUI.Color = NadTheme.Color.ember

    /// Sample count around the blob. Must stay well above twice the highest harmonic
    /// in `wobble` below: at the old value of 10 the 5th harmonic sat exactly on
    /// Nyquist, aliased, and the smoothing rendered it as hard spikes on the outer
    /// edge rather than lobes.
    private let pointCount = 96
    private let rippleDuration: TimeInterval = 1.1
    /// How far a ripple travels, as a multiple of `baseRadius`. Bounded by the frame:
    /// anything past `1 / 0.25 * 0.5 = 2.0` would hit the Canvas clip.
    private let rippleReach: CGFloat = 0.8

    private struct Ripple: Identifiable {
        let id = UUID()
        let start: Date
    }

    @State private var ripples: [Ripple] = []

    var body: some View {
        TimelineView(.animation) { timeline in
            Canvas { context, size in
                let center = CGPoint(x: size.width / 2, y: size.height / 2)
                // Canvas clips to its bounds, so every effect has to fit inside the
                // frame or it gets sliced off square. The widest thing drawn is a
                // fully-expanded ripple at `baseRadius * rippleReach`; this fraction
                // keeps that (and the glow) clear of the edge with room to spare.
                let baseRadius = min(size.width, size.height) * 0.25
                let time = timeline.date.timeIntervalSinceReferenceDate

                for ripple in ripples {
                    draw(ripple, at: center, baseRadius: baseRadius, now: timeline.date, in: &context)
                }

                let blob = blobPath(center: center, baseRadius: baseRadius, time: time)

                // Soft halo first, so the rim falls off instead of ending on a hard
                // edge, then the crisp body on top.
                var halo = context
                halo.addFilter(.blur(radius: baseRadius * 0.26))
                halo.fill(blob, with: .color(accent.opacity(0.35)))

                context.addFilter(.shadow(color: accent.opacity(0.5), radius: baseRadius * 0.30))
                context.fill(
                    blob,
                    with: .radialGradient(
                        Gradient(colors: [accent, accent.opacity(0.72)]),
                        center: center,
                        startRadius: 0,
                        // Completes inside the shape; ending beyond it clipped the
                        // gradient and left a visible step at the edge.
                        endRadius: baseRadius
                    )
                )
            }
        }
        .onChange(of: rippleTrigger) { _, _ in
            let ripple = Ripple(start: Date())
            ripples.append(ripple)
            Task {
                try? await Task.sleep(for: .seconds(rippleDuration))
                ripples.removeAll { $0.id == ripple.id }
            }
        }
        .accessibilityElement()
        .accessibilityLabel("Voice activity")
    }

    private func draw(
        _ ripple: Ripple,
        at center: CGPoint,
        baseRadius: CGFloat,
        now: Date,
        in context: inout GraphicsContext
    ) {
        let elapsed = now.timeIntervalSince(ripple.start)
        guard elapsed >= 0, elapsed <= rippleDuration else { return }
        let progress = elapsed / rippleDuration
        let radius = baseRadius + CGFloat(progress) * baseRadius * rippleReach
        let opacity = (1 - progress) * 0.5
        let rect = CGRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
        context.stroke(
            Path(ellipseIn: rect),
            with: .color(accent.opacity(opacity)),
            lineWidth: max(0.75, baseRadius * 0.026)
        )
    }

    private func blobPath(center: CGPoint, baseRadius: CGFloat, time: Double) -> Path {
        // Proportional, so the shape reads the same at 200pt and at 22pt. Also much
        // tamer than the old absolute swing, which reached ±62% of the radius.
        let amplitude = (0.06 + CGFloat(level) * 0.26) * baseRadius
        // Keeps the form alive when there's no audio at all.
        let breath = baseRadius * (1 + 0.02 * CGFloat(sin(time * 0.55)))

        var points: [CGPoint] = []
        points.reserveCapacity(pointCount)
        for i in 0 ..< pointCount {
            let angle = (Double(i) / Double(pointCount)) * 2 * .pi
            let wobble = sin(angle * 3 + time * 1.3) * 0.5 + sin(angle * 5 - time * 0.85) * 0.5
            let radius = breath + amplitude * CGFloat(wobble)
            points.append(CGPoint(x: center.x + cos(angle) * radius, y: center.y + sin(angle) * radius))
        }
        return Self.smoothClosedPath(through: points)
    }

    /// Catmull-Rom through every sample, converted to the equivalent cubic Béziers.
    /// C¹-continuous, so there is no flattening or cusp at the joins regardless of how
    /// far a point is pushed out.
    private static func smoothClosedPath(through points: [CGPoint]) -> Path {
        var path = Path()
        guard points.count > 2 else { return path }

        let count = points.count
        path.move(to: points[0])
        for i in 0 ..< count {
            let p0 = points[(i - 1 + count) % count]
            let p1 = points[i]
            let p2 = points[(i + 1) % count]
            let p3 = points[(i + 2) % count]

            let control1 = CGPoint(x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6)
            let control2 = CGPoint(x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6)
            path.addCurve(to: p2, control1: control1, control2: control2)
        }
        path.closeSubpath()
        return path
    }
}

#Preview {
    VStack(spacing: 40) {
        VoiceVisualizer(level: 0.4, rippleTrigger: 0)
            .frame(width: 220, height: 220)
        VoiceVisualizer(level: 0.4, rippleTrigger: 0)
            .frame(width: 22, height: 22)
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(NadTheme.Color.void)
}
