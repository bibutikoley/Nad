//
//  VoiceSessionController.swift
//  nad-ios
//
//  Wrapper around LiveKit's Session. Views observe *this* object only — never
//  `session` directly. That matters: SwiftUI has no way to observe a nested
//  ObservableObject reached through a plain `let`, so a view that reads
//  `controller.session.agent` renders whatever was true at its last invalidation and
//  silently goes stale. Everything a view needs is republished here as `phase` and
//  `messages`, alongside what Session doesn't provide at all: audio-level metering,
//  mic control, and mic-permission handling.
//
//  Deliberately uses `Session(tokenSource:tokenOptions:)`, never
//  `Session.withAgent(_:)` — see NadTokenSource.swift for why.
//

import AVFAudio
import Combine
import LiveKit

@MainActor
final class VoiceSessionController: ObservableObject {

    /// What the user is actually allowed to believe about the session.
    ///
    /// `Session.isConnected` is true for `.connecting` and `.reconnecting` as well as
    /// `.connected`, so it can't be used to decide whether the agent can hear anything.
    /// This splits that coarse flag into the states the UI needs to distinguish.
    enum Phase: Equatable {
        /// Never started, or ended.
        case idle
        /// Fetching a token / joining the room. No agent participant yet.
        case connecting
        /// The agent is in the room but still loading its models.
        case warmingUp
        /// The agent is genuinely ready. Only now is the mic open.
        case ready(AgentState)
        case failed(String)

        var isReady: Bool {
            if case .ready = self { return true }
            return false
        }
    }

    let session: Session
    let store: ConversationStore

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var messages: [ReceivedMessage] = []

    /// The conversation being recorded right now. Carries the id of the resumed
    /// conversation when there is one, so continuing an old thread keeps appending to
    /// that same history entry instead of forking a near-duplicate.
    private var activeConversation: Conversation?

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

    /// The mic is held closed from connect until the agent first reports ready, so the
    /// app never captures audio while telling the user it isn't listening.
    private var micReleased = false
    private var readinessTimeout: Task<Void, Never>?
    private var failureMessage: String?

    /// Covers a backend that never shows up. Session's own `agentConnectTimeout` only
    /// arms when the token response requests explicit agent dispatch; nad-backend uses
    /// implicit dispatch, so that timer never fires and a dead worker would otherwise
    /// hang here forever. Generous, because a first-ever model download is slow.
    private let readinessTimeoutSeconds: UInt64 = 90

    private let settings: AppSettings

    init(settings: AppSettings, store: ConversationStore) {
        self.settings = settings
        self.store = store
        session = Session(
            tokenSource: NadTokenSource(settings: settings),
            // preConnectAudio is deliberately off: its purpose is to buffer speech
            // *before* the agent joins, which is exactly what we don't want when the
            // mic is held closed until the agent is ready.
            options: SessionOptions(preConnectAudio: false, agentConnectTimeout: 20)
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

        // Session's `connectionState` is @Published but private, so there's no
        // per-property publisher to observe for connection changes. objectWillChange
        // covers every one of them; it fires *before* the mutation, so the hop through
        // the main queue is what makes the read below see post-change values.
        session.objectWillChange
            .receive(on: DispatchQueue.main)
            .sink { [weak self] in
                self?.syncFromSession()
            }
            .store(in: &cancellables)
    }

    private func rewireAgentAudioTrack(_ track: (any AudioTrack)?) {
        guard (currentAgentAudioTrack as AnyObject?) !== (track as AnyObject?) else { return }
        currentAgentAudioTrack?.remove(audioRenderer: agentLevelMonitor)
        currentAgentAudioTrack = track
        track?.add(audioRenderer: agentLevelMonitor)
    }

    // MARK: - Derived state

    private func syncFromSession() {
        messages = session.messages
        recordTranscript()

        // Latch failures. Reporting one usually means tearing the room down, and the
        // teardown clears the SDK's own error — so reading it live would flash the
        // banner and immediately drop back to a bare idle screen with no explanation.
        if let error = session.error {
            failureMessage = error.localizedDescription
        } else if let agentError = session.agent.error {
            failureMessage = Self.message(for: agentError)
        }

        let resolved = resolvePhase()
        guard resolved != phase else { return }
        phase = resolved

        if resolved.isReady {
            readinessTimeout?.cancel()
            readinessTimeout = nil
            releaseMicrophoneIfNeeded()
        }
    }

    private func resolvePhase() -> Phase {
        if let failureMessage {
            return .failed(failureMessage)
        }
        guard session.isConnected else { return .idle }

        guard let agentState = session.agent.agentState else {
            // Room is up but no agent participant has joined yet.
            return .connecting
        }
        switch agentState {
        case .listening, .thinking, .speaking:
            return .ready(agentState)
        case .idle, .initializing:
            return .warmingUp
        }
    }

    /// Saves as the conversation happens rather than only at the end, so a crash or a
    /// swipe-to-quit mid-conversation doesn't lose it. The store debounces the writes.
    private func recordTranscript() {
        guard var conversation = activeConversation, !messages.isEmpty else { return }
        guard conversation.messages != messages else { return }
        conversation.messages = messages
        conversation.updatedAt = Date()
        activeConversation = conversation
        store.upsert(conversation)
    }

    private static func message(for error: Agent.Error) -> String {
        switch error {
        case .timeout: "No agent joined. Is scripts/dev.sh running on the backend Mac?"
        case .left: "The agent left the room unexpectedly."
        }
    }

    // MARK: - Lifecycle

    /// - Parameter resuming: a saved conversation to continue. Its transcript is parked
    ///   on the token server before connecting so the agent can seed its chat context
    ///   with it — the agent genuinely remembers it, rather than merely being shown
    ///   alongside its transcript.
    func start(resuming resumed: Conversation? = nil) async {
        await requestMicPermissionIfNeeded()
        guard !micPermissionDenied else { return }

        micReleased = false
        failureMessage = nil
        session.dismissError()
        phase = .connecting

        if let resumed {
            let room = ConversationHandoff.makeRoomName()
            do {
                try await ConversationHandoff.park(
                    resumed, forRoom: room, baseURL: settings.effectiveBaseURL
                )
            } catch {
                // Connecting anyway would silently drop the memory the user asked for,
                // so stop here and say so instead.
                failureMessage = error.localizedDescription
                phase = .failed(error.localizedDescription)
                return
            }
            settings.oneShotRoomName = room
            activeConversation = resumed
            session.restoreMessageHistory(resumed.messages)
            messages = resumed.messages
        } else {
            activeConversation = Conversation()
        }

        await session.start()
        // Only ever applies to the connection it was set for.
        settings.oneShotRoomName = nil

        // Session.start() enables the mic itself once the room is up. Close it again
        // immediately: the agent still has models to load and can't hear anything yet.
        if !phase.isReady {
            _ = try? await session.room.localParticipant.setMicrophone(enabled: false)
        }

        startReadinessTimeout()
    }

    func end() async {
        readinessTimeout?.cancel()
        readinessTimeout = nil
        await session.end()

        recordTranscript()
        activeConversation = nil
        settings.oneShotRoomName = nil
        await store.flush()

        // Reset everything the SDK doesn't. Session clears its own agent state on
        // disconnect, but these are ours, and a stale mic level or "muted" icon
        // carried into the next session misrepresents what the app is doing.
        failureMessage = nil
        session.dismissError()

        // Session keeps its message history across a disconnect, so without this the
        // previous conversation is still on screen when the next one starts.
        session.restoreMessageHistory([])
        messages = []

        phase = .idle
        isMicMuted = false
        micReleased = false
        micLevel = 0
        agentLevel = 0
        micLevelMonitor.reset()
        agentLevelMonitor.reset()
    }

    func dismissError() {
        failureMessage = nil
        session.dismissError()
        phase = resolvePhase()
    }

    private func startReadinessTimeout() {
        readinessTimeout?.cancel()
        let seconds = readinessTimeoutSeconds
        readinessTimeout = Task { [weak self] in
            try? await Task.sleep(for: .seconds(seconds))
            guard !Task.isCancelled, let self, !self.phase.isReady else { return }
            self.failureMessage = "No agent joined. Is scripts/dev.sh running on the backend Mac?"
            self.phase = .failed(self.failureMessage!)
            // Tear the room down so we don't sit in a half-open session holding a mic.
            await self.session.end()
            self.micLevelMonitor.reset()
            self.micLevel = 0
        }
    }

    // MARK: - Microphone

    private func releaseMicrophoneIfNeeded() {
        guard !micReleased else { return }
        micReleased = true
        Task { [weak self] in
            guard let self, !self.isMicMuted else { return }
            _ = try? await self.session.room.localParticipant.setMicrophone(enabled: true)
        }
    }

    func toggleMicrophone() async {
        let shouldEnable = isMicMuted
        do {
            try await session.room.localParticipant.setMicrophone(enabled: shouldEnable)
            isMicMuted.toggle()
            if isMicMuted {
                micLevelMonitor.reset()
                micLevel = 0
            }
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
