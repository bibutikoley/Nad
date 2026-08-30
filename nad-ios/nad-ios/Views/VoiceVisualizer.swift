//
//  VoiceVisualizer.swift
//  nad-ios
//
//  The signature element. "Nad" (नाद) — sound, resonance — rendered as one
//  continuous organic form that swells and wobbles with live audio level, not a row
//  of EQ bars. The form itself is the whole idea: it breathes while idle and pulses
//  with whoever is speaking, so the voice is legible without any chrome around it.
//
//  Every dimension is derived from the frame it's given, so the same view is both the
//  large stage blob and the compact one that sits beside the session buttons.
//

import SwiftUI

struct VoiceVisualizer: View {
    /// Combined, smoothed 0...1 audio level (mic while listening, agent while speaking).
    var level: Float
    var accent: SwiftUI.Color = NadTheme.Color.ember
    /// Resting radius as a fraction of the frame. The frame also has to absorb the swell,
    /// the wobble and the glow — see `clipBudget`, which caps this at about 0.24 — so the
    /// visible body is always well under half the frame it is given.
    var radiusRatio: CGFloat = 0.24

    /// Sample count around the blob. Must stay well above twice the highest harmonic
    /// in `wobble` below: at the old value of 10 the 5th harmonic sat exactly on
    /// Nyquist, aliased, and the smoothing rendered it as hard spikes on the outer
    /// edge rather than lobes.
    private let pointCount = 96

    /// How far a full-level voice swells the body, as a fraction of the resting radius.
    /// This is the motion a viewer reads as "it's hearing me" — the edge wobble alone never
    /// did, because the blob's size never changed.
    ///
    /// Small on purpose. This sits in a row of static controls, so it should suggest that
    /// something is happening, not compete for attention; past roughly 0.12 it stops
    /// looking like breathing and starts looking unstable.
    private let maxSwell: CGFloat = 0.10
    /// Peak edge deformation at full level, same units. Kept close to the swell so the form
    /// stays recognisably round and simply softens, rather than thrashing between shapes.
    private let maxWobble: CGFloat = 0.09
    /// How far the glow bleeds past the rim, same units.
    private let glowReach: CGFloat = 0.30

    /// Everything the Canvas has to fit, in resting radii. `Canvas` clips to its bounds, so
    /// `radiusRatio` must stay under `0.5 / clipBudget` or a fully swollen blob gets sliced
    /// off square. Raising `maxSwell` or `maxWobble` tightens that ceiling — check it here
    /// before nudging either of them.
    private var clipBudget: CGFloat { 1 + maxSwell + maxWobble + glowReach }

    /// Level, eased so the blob leans into ordinary speech rather than only reacting to
    /// shouts. `AudioLevelMonitor` smooths in time; this only reshapes the curve.
    ///
    /// Measured levels sit below full scale, so some lift is needed — but not much. A
    /// harder curve (0.4 and below) also magnifies the noise floor, which is what makes an
    /// idle blob twitch at nothing.
    private var eased: CGFloat {
        let clamped = min(max(CGFloat(level), 0), 1)
        return sqrt(clamped)
    }

    /// Shared, because the radial gradient has to end exactly on the swollen rim —
    /// ending short of it leaves a visible step.
    private var swell: CGFloat { 1 + maxSwell * eased }

    var body: some View {
        TimelineView(.animation) { timeline in
            Canvas { context, size in
                let center = CGPoint(x: size.width / 2, y: size.height / 2)
                let baseRadius = min(size.width, size.height) * radiusRatio
                let time = timeline.date.timeIntervalSinceReferenceDate

                let blob = blobPath(center: center, baseRadius: baseRadius, time: time)

                // Soft halo first, so the rim falls off instead of ending on a hard
                // edge, then the crisp body on top. Both brighten with level: the glow
                // carries much of the sense of loudness, since the body can only swell so
                // far before it runs out of frame.
                var halo = context
                halo.addFilter(.blur(radius: baseRadius * 0.26))
                halo.fill(blob, with: .color(accent.opacity(0.32 + 0.18 * eased)))

                context.addFilter(
                    .shadow(
                        color: accent.opacity(0.5 + 0.2 * eased),
                        radius: baseRadius * glowReach * (0.85 + 0.15 * eased)
                    )
                )
                context.fill(
                    blob,
                    with: .radialGradient(
                        Gradient(colors: [accent, accent.opacity(0.72)]),
                        center: center,
                        startRadius: 0,
                        // Completes on the rim; ending short of it (or beyond it) leaves a
                        // visible step, so it has to track the swell.
                        endRadius: baseRadius * swell
                    )
                )
            }
        }
        .accessibilityElement()
        .accessibilityLabel("Voice activity")
    }

    private func blobPath(center: CGPoint, baseRadius: CGFloat, time: Double) -> Path {
        // Proportional, so the shape reads the same at 200pt and at 26pt.
        let amplitude = (0.03 + eased * maxWobble) * baseRadius
        // Two motions layered: `swell` is the pulse that tracks the voice, and the slow
        // sine keeps the form alive when there is no audio at all.
        let breath = baseRadius * swell * (1 + 0.02 * CGFloat(sin(time * 0.55)))

        // The lobes drift at a fixed, slow rate. An earlier version scaled this with the
        // level so a loud voice read as "agitated"; in practice it just looked unstable,
        // because the level and the tempo were both moving at once. Size and glow carry
        // the loudness now, and the drift stays calm underneath them.
        var points: [CGPoint] = []
        points.reserveCapacity(pointCount)
        for i in 0 ..< pointCount {
            let angle = (Double(i) / Double(pointCount)) * 2 * .pi
            let wobble = sin(angle * 3 + time * 0.5) * 0.5 + sin(angle * 5 - time * 0.33) * 0.5
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
        VoiceVisualizer(level: 0.0)
            .frame(width: 156, height: 156)
        VoiceVisualizer(level: 0.5)
            .frame(width: 156, height: 156)
        VoiceVisualizer(level: 1.0)
            .frame(width: 156, height: 156)
        VoiceVisualizer(level: 0.4)
            .frame(width: 220, height: 220)
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(NadTheme.Color.void)
}
