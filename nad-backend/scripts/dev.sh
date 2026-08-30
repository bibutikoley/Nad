#!/usr/bin/env bash
# Runs the three native dev processes — the mlx-audio speech server (TTS), the STT
# server, and the agent worker — in one terminal instead of three. Ctrl+C, or any one
# of them exiting on its own (crash), stops the rest (and everything each one spawned).
#
# LiveKit + the token server run separately via `docker compose up` — see README.md.
set -o pipefail
cd "$(dirname "$0")/.."

# `lk` (the LiveKit CLI driving `lk agent dev` below) reads LIVEKIT_URL /
# LIVEKIT_API_KEY / LIVEKIT_API_SECRET from its own process environment before
# it ever imports agent.py — agent.py's own load_dotenv() runs too late for lk's
# purposes, so they're exported here instead.
[ -f .env ] || { echo "nad-backend/.env not found — copy .env.example to .env first." >&2; exit 1; }
set -a
# shellcheck source=/dev/null
source .env
set +a

pids=()
names=()
last_known=""   # snapshot of every live descendant, refreshed each poll — see below

# Lists every (transitive) child of a pid — `uvx`, and `lk agent dev` (which
# itself shells out through `uv run`), each fork a child to actually run the
# tool rather than exec'ing into it, so the top-level PID alone isn't enough to
# reach the real speech server / agent process.
descendants_of() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    echo "$child"
    descendants_of "$child"
  done
}

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "Stopping…"
  # Re-walk the tree now, but also fall back to the last snapshot taken before
  # this run — if a process was SIGKILLed outright (e.g. by the OOM killer) its
  # children are orphaned instantly and no longer show up under it in a fresh walk.
  local targets
  targets=$(
    for pid in "${pids[@]}"; do echo "$pid"; descendants_of "$pid"; done
    echo "$last_known"
  )
  # SIGINT first — the same signal Ctrl+C would send each process directly, so it
  # gets the graceful shutdown path it already handles.
  for pid in $targets; do
    kill -INT "$pid" 2>/dev/null
  done
  sleep 2
  # Anything still standing after 2s (stuck, or ignoring SIGINT) gets killed outright.
  for pid in $targets; do
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
  done
  exit 0
}
trap cleanup INT TERM EXIT

scripts/speech-server.sh &
pids+=("$!"); names+=("speech server")

# Unconditional, so STT_BASE_URL always has something listening. This was gated on
# STT_BACKEND being set, which made deleting that one line from .env fail as a connection
# refused on the first spoken turn rather than at startup — everything came up looking
# healthy and only the first sentence found the hole. The default STT model can't be
# served by anything else, so the server is part of the stack, not an option.
#
# Before the agent, so it is already binding while the worker boots and its warm-up
# finds a listener rather than a refused connection.
scripts/stt-server.sh &
pids+=("$!"); names+=("stt server (${STT_BACKEND:-omi})")

lk agent dev agent.py &
pids+=("$!"); names+=("agent worker")

# Poll rather than `wait -n` (bash 4.3+ only — macOS ships bash 3.2 at /bin/bash):
# stop everything the moment either process exits on its own, the same way you'd
# notice a crash in one of two terminals and go stop the other.
while :; do
  # Accumulate, don't overwrite: once a pid is seen it stays a cleanup target even
  # after its parent dies and a later walk can no longer reach it (see cleanup()).
  last_known=$(
    { echo "$last_known"; for pid in "${pids[@]}"; do echo "$pid"; descendants_of "$pid"; done; } \
      | sort -u
  )
  for i in "${!pids[@]}"; do
    if ! kill -0 "${pids[$i]}" 2>/dev/null; then
      echo "${names[$i]} exited — stopping the rest"
      exit 0
    fi
  done
  sleep 1
done
