//
//  VoiceSessionController.swift
//  nad-ios
//
//  Thin wrapper around LiveKit's Session. `Session` is itself @MainActor +
//  ObservableObject with published `agent`, `messages`, `error` — views observe
//  `session` directly (its objectWillChange fires even though the backing
//  `connectionState` is private). This controller adds what Session doesn't:
//  audio-level metering for the visualizer, mic mute, and mic-permission handling.
//
//  Deliberately uses `Session(tokenSource:tokenOptions:)`, never
//  `Session.withAgent(_:)` — see NadTokenSource.swift for why.
//

import AVFAudio
import Combine
import LiveKit

@MainActor
final class VoiceSessionController: ObservableObject {
    let session: Session

    @Published private(set) var isMicMuted = false
    @Published private(set) var micPermissionDenied = false
    @Published var micToggleError: String?

    /// Smoothed 0...1 levels for the visualizer.
    @Published private(set) var micLevel: Float = 0
    @Published private(set) var agentLevel: Float = 0

    private let micLevelMonitor = AudioLevelMonitor()
    private let agentLevelMonitor = AudioLevelMonitor()
    private var currentAgentAudioTrack: (any AudioTrack)?
    private var cancellables = Set<AnyCancellable>()

    init(settings: AppSettings) {
        session = Session(
            tokenSource: NadTokenSource(settings: settings),
            options: SessionOptions(preConnectAudio: true, agentConnectTimeout: 20)
        )
        AudioManager.shared.add(localAudioRenderer: micLevelMonitor)
        observe()
    }

    deinit {
        AudioManager.shared.remove(localAudioRenderer: micLevelMonitor)
    }

    private func observe() {
        micLevelMonitor.$level
            .receive(on: DispatchQueue.main)
            .assign(to: &$micLevel)

        agentLevelMonitor.$level
            .receive(on: DispatchQueue.main)
            .assign(to: &$agentLevel)

        session.$agent
            .receive(on: DispatchQueue.main)
            .sink { [weak self] agent in
                self?.rewireAgentAudioTrack(agent.audioTrack)
            }
            .store(in: &cancellables)
    }

    private func rewireAgentAudioTrack(_ track: (any AudioTrack)?) {
        guard (currentAgentAudioTrack as AnyObject?) !== (track as AnyObject?) else { return }
        currentAgentAudioTrack?.remove(audioRenderer: agentLevelMonitor)
        currentAgentAudioTrack = track
        track?.add(audioRenderer: agentLevelMonitor)
    }

    // MARK: - Lifecycle

    func start() async {
        await requestMicPermissionIfNeeded()
        guard !micPermissionDenied else { return }
        await session.start()
    }

    func end() async {
        await session.end()
    }

    // MARK: - Microphone

    func toggleMicrophone() async {
        let shouldEnable = isMicMuted
        do {
            try await session.room.localParticipant.setMicrophone(enabled: shouldEnable)
            isMicMuted.toggle()
        } catch {
            micToggleError = error.localizedDescription
        }
    }

    private func requestMicPermissionIfNeeded() async {
        switch AVAudioApplication.shared.recordPermission {
        case .granted:
            micPermissionDenied = false
        case .denied:
            micPermissionDenied = true
        case .undetermined:
            let granted = await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
            micPermissionDenied = !granted
        @unknown default:
            micPermissionDenied = true
        }
    }

    // MARK: - Text

    @discardableResult
    func sendText(_ text: String) async -> SentMessage? {
        await session.send(text: text)
    }
}
