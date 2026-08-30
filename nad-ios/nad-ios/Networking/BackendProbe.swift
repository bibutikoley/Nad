//
//  BackendProbe.swift
//  nad-ios
//
//  Drives the Settings screen's "Test Connection" flow: three independent checks,
//  each with its own actionable failure message, rather than one opaque
//  connect-or-fail. Mirrors the actual signal path in nad-backend/README.md's
//  topology diagram: token server reachable → bearer token accepted → LiveKit
//  reachable. Step 3 matters on its own because LIVEKIT_URL is a *different*
//  host/port than the token server and is the one that goes stale on DHCP renewal.
//

import Combine
import Foundation
import LiveKit

enum ProbeStepStatus: Equatable {
    case pending
    case running
    case success(detail: String)
    case failure(message: String)
}

struct ProbeStep: Identifiable, Equatable {
    let id: String
    let title: String
    var status: ProbeStepStatus = .pending
}

@MainActor
final class BackendProbe: ObservableObject {
    @Published private(set) var steps: [ProbeStep] = [
        ProbeStep(id: "health", title: "Token server reachable"),
        ProbeStep(id: "token", title: "Bearer token accepted"),
        ProbeStep(id: "livekit", title: "LiveKit reachable"),
    ]
    @Published private(set) var isRunning = false

    private let settings: AppSettings

    init(settings: AppSettings) {
        self.settings = settings
    }

    func run() async {
        guard !isRunning else { return }
        isRunning = true
        defer { isRunning = false }

        reset()
        let baseURL = settings.effectiveBaseURL

        await step("health") {
            let (elapsed, (_, http)) = try await timed {
                try await Self.get(baseURL.appendingPathComponent("health"))
            }
            guard (200 ..< 300).contains(http.statusCode) else {
                throw NadTokenSourceError.malformedResponse("HTTP \(http.statusCode)")
            }
            return "\(baseURL.host ?? baseURL.absoluteString) · \(elapsed)ms"
        }
        guard case .success = steps[0].status else { return }

        var liveKitURLString = ""
        await step("token") {
            let (elapsed, (data, http)) = try await timed {
                var request = URLRequest(url: baseURL.appendingPathComponent("token")
                    .appending(queryItems: [URLQueryItem(name: "room", value: "nad-connection-test")]))
                request.setValue("Bearer \(BackendConfig.authToken)", forHTTPHeaderField: "Authorization")
                return try await Self.get(request: request)
            }
            if http.statusCode == 401 {
                throw NadTokenSourceError.unauthorized
            }
            guard (200 ..< 300).contains(http.statusCode) else {
                throw NadTokenSourceError.malformedResponse("HTTP \(http.statusCode)")
            }
            struct Resp: Decodable { let url: String }
            let decoded = try JSONDecoder().decode(Resp.self, from: data)
            liveKitURLString = decoded.url
            return "\(elapsed)ms · \(decoded.url)"
        }
        guard case .success = steps[1].status else { return }

        await step("livekit") {
            guard var components = URLComponents(string: liveKitURLString), let host = components.host else {
                throw NadTokenSourceError.invalidLiveKitURL(liveKitURLString)
            }
            components.scheme = (components.scheme == "wss") ? "https" : "http"
            guard let httpURL = components.url else {
                throw NadTokenSourceError.invalidLiveKitURL(liveKitURLString)
            }
            // LiveKit's signalling port has no /health route; any HTTP reply at all
            // (even a 4xx from the WS upgrade handler) proves the host:port is up.
            let (elapsed, _) = try await timed {
                try await Self.get(httpURL)
            }
            return "\(host):\(components.port.map(String.init) ?? "-") · \(elapsed)ms"
        }
    }

    private func reset() {
        steps = steps.map { ProbeStep(id: $0.id, title: $0.title, status: .pending) }
    }

    /// How long each step's "running" state is held on screen at minimum. On a LAN
    /// these checks finish in single-digit milliseconds, so without this the three
    /// rows resolve in one frame and you never see which step was doing what.
    /// Presentation only — the latency each step reports is measured by `timed`
    /// around the actual request, so the numbers stay truthful.
    private static let minimumVisibleRunning: Duration = .milliseconds(320)

    private func step(_ id: String, _ work: () async throws -> String) async {
        setStatus(.running, for: id)
        let startedAt = ContinuousClock.now

        let outcome: ProbeStepStatus
        do {
            outcome = .success(detail: try await work())
        } catch {
            outcome = .failure(message: error.localizedDescription)
        }

        let elapsed = ContinuousClock.now - startedAt
        if elapsed < Self.minimumVisibleRunning {
            try? await Task.sleep(for: Self.minimumVisibleRunning - elapsed)
        }
        setStatus(outcome, for: id)
    }

    private func setStatus(_ status: ProbeStepStatus, for id: String) {
        guard let index = steps.firstIndex(where: { $0.id == id }) else { return }
        steps[index].status = status
    }

    private func timed<T>(_ work: () async throws -> T) async throws -> (Int, T) {
        let start = DispatchTime.now()
        let result = try await work()
        let elapsedMs = Int(Double(DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1_000_000)
        return (elapsedMs, result)
    }

    private static func get(_ url: URL) async throws -> (Data, HTTPURLResponse) {
        try await get(request: URLRequest(url: url))
    }

    private static func get(request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw NadTokenSourceError.malformedResponse("no HTTP status")
            }
            return (data, http)
        } catch let error as NadTokenSourceError {
            throw error
        } catch {
            throw NadTokenSourceError.unreachable(Underlying: error)
        }
    }
}
