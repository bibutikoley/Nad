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

import SwiftUI

struct VoiceVisualizer: View {
    /// Combined, smoothed 0...1 audio level (mic while listening, agent while speaking).
    var level: Float
    /// Bump this to fire one outward ripple — call on each speech-onset edge.
    var rippleTrigger: Int
    var accent: SwiftUI.Color = NadTheme.Color.ember

    private let baseRadius: CGFloat = 58
    private let pointCount = 10
    private let rippleDuration: TimeInterval = 1.1

    private struct Ripple: Identifiable {
        let id = UUID()
        let start: Date
    }

    @State private var ripples: [Ripple] = []

    var body: some View {
        TimelineView(.animation) { timeline in
            Canvas { context, size in
                let center = CGPoint(x: size.width / 2, y: size.height / 2)
                let time = timeline.date.timeIntervalSinceReferenceDate

                for ripple in ripples {
                    draw(ripple, at: center, now: timeline.date, in: &context)
                }

                let blob = blobPath(center: center, time: time)
                context.addFilter(.shadow(color: accent.opacity(0.5), radius: 28))
                context.fill(
                    blob,
                    with: .radialGradient(
                        Gradient(colors: [accent, accent.opacity(0.72)]),
                        center: center,
                        startRadius: 0,
                        endRadius: baseRadius * 1.5
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

    private func draw(_ ripple: Ripple, at center: CGPoint, now: Date, in context: inout GraphicsContext) {
        let elapsed = now.timeIntervalSince(ripple.start)
        guard elapsed >= 0, elapsed <= rippleDuration else { return }
        let progress = elapsed / rippleDuration
        let radius = baseRadius + CGFloat(progress) * baseRadius * 1.9
        let opacity = (1 - progress) * 0.5
        let rect = CGRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
        context.stroke(Path(ellipseIn: rect), with: .color(accent.opacity(opacity)), lineWidth: 1.5)
    }

    private func blobPath(center: CGPoint, time: Double) -> Path {
        let amplitude = 6 + CGFloat(level) * 30
        var points: [CGPoint] = []
        points.reserveCapacity(pointCount)
        for i in 0 ..< pointCount {
            let angle = (Double(i) / Double(pointCount)) * 2 * .pi
            let wobble = sin(angle * 3 + time * 1.3) * 0.5 + sin(angle * 5 - time * 0.85) * 0.5
            let radius = baseRadius + amplitude * CGFloat(wobble)
            points.append(CGPoint(x: center.x + cos(angle) * radius, y: center.y + sin(angle) * radius))
        }
        return Self.smoothClosedPath(through: points)
    }

    /// Catmull-Rom-ish smoothing: a quad curve per edge, using each source point as
    /// the curve's control and the midpoint to the next point as its anchor. Gives a
    /// continuous, gently lobed blob instead of a faceted polygon.
    private static func smoothClosedPath(through points: [CGPoint]) -> Path {
        var path = Path()
        guard points.count > 2 else { return path }
        func midpoint(_ a: CGPoint, _ b: CGPoint) -> CGPoint {
            CGPoint(x: (a.x + b.x) / 2, y: (a.y + b.y) / 2)
        }
        path.move(to: midpoint(points[points.count - 1], points[0]))
        for i in 0 ..< points.count {
            let current = points[i]
            let next = points[(i + 1) % points.count]
            path.addQuadCurve(to: midpoint(current, next), control: current)
        }
        path.closeSubpath()
        return path
    }
}

#Preview {
    VoiceVisualizer(level: 0.4, rippleTrigger: 0)
        .frame(width: 220, height: 220)
        .background(NadTheme.Color.void)
}
