# nad-ios

The voice client for [nad-backend](../nad-backend). SwiftUI + the
[LiveKit Swift SDK](https://github.com/livekit/client-sdk-swift)'s `Session` agent API —
connect, talk, watch a live transcript, barge in mid-reply.

## First-time setup

1. Have `nad-backend` running (`docker compose up` + `scripts/dev.sh` — see that project's
   README).
2. Copy the backend config template and fill in two values:

   ```bash
   cd nad-ios/nad-ios/Config
   cp BackendConfig.example.swift.txt BackendConfig.swift
   ```

   Both come from `nad-backend/.env` on the machine running the backend:
   - `baseURL` — `http://<that Mac's LAN IP>:<TOKEN_SERVER_PORT>`, e.g. `http://192.168.1.23:8787`.
     Find the IP the same way the backend README does: `ipconfig getifaddr en0`.
   - `authToken` — `TOKEN_SERVER_AUTH_TOKEN`, verbatim.

   `BackendConfig.swift` is git-ignored (same shape as `nad-backend/.env` vs `.env.example`) —
   the real bearer secret never lands in the repo. Whoever builds the app fills in their own copy.
3. Open `nad-ios.xcodeproj` in Xcode (26.3+; the LiveKit SDK needs Swift tools 6.1) and run.
   The Swift Package dependency resolves automatically on first build.

## What's configured where

- **`BackendConfig.swift`** (compiled in, git-ignored): the token server's base URL and the
  `TOKEN_SERVER_AUTH_TOKEN` bearer secret. Not editable from the app — this is server config,
  not a per-device preference, and the secret should never be visible in the UI.
- **Settings screen (in-app)**: a *runtime override* for the base URL only — useful when the
  Mac's LAN IP changes (DHCP renewal) and you don't want to rebuild — plus optional room name /
  participant identity overrides, and a three-step "Run test" that checks the token server, the
  bearer token, and LiveKit itself, independently.
- **The LiveKit join token** (the JWT `token_server.py` mints per-session) never appears
  anywhere in the app. It's fetched, handed straight to the SDK, and discarded — see
  `Networking/NadTokenSource.swift`.

## Structure

```
Config/       BackendConfig (compiled secrets), AppSettings (runtime overrides)
Networking/   NadTokenSource (bridges token_server.py's response shape to the SDK),
              BackendProbe (the Settings connection test)
Session/      VoiceSessionController (wraps LiveKit's Session), AudioLevelMonitor
Views/        VoiceView (main screen), VoiceVisualizer (the audio-reactive blob),
              TranscriptView, ComposerView, SettingsView, ConnectionTestRow
DesignSystem/ Theme.swift — palette, type scale, spacing, motion
```

`VoiceSessionController` uses `Session(tokenSource:tokenOptions:)`, never
`Session.withAgent(_:)` — the latter requests explicit agent dispatch, and `agent.py` in
nad-backend registers with no agent name (implicit dispatch), so an explicitly-dispatched
session would never be joined.

## Design

Dark, single-accent (warm amber/copper — deliberately not the usual AI blue/violet), SF Pro for
conversation vs. SF Mono for system state. The visualizer is one continuous organic shape that
wobbles with live audio level rather than a bar-style EQ, with a ripple that propagates outward
on each speech onset — a literal nod to "Nad" (नाद), Sanskrit for sound/resonance.

## Known gaps

- No physical-device pass yet beyond the Simulator — the ATS/local-network entitlements are in
  place (see `nad-backend/README.md`), but confirm the local-network permission prompt and
  actual audio flow on a real iPhone on the same Wi-Fi before relying on it.
- Per-user auth is still a shared bearer secret, same caveat as `nad-backend` — see that
  project's README under "Notes".
