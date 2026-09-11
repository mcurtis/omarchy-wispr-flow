#!/bin/bash
# Drive the Wispr Flow bar widget without Wispr Flow: point the plugin at a
# fake launcher log and walk it through a push-to-talk cycle with realistic
# timing. The original logPath setting is restored on exit, including Ctrl-C.
#
# Listening comes from the fake log here (Wispr's PipeWire stream is absent),
# and the meter samples your real default microphone: speak or play audio
# while holding `listening` if you want visible bars.

set -euo pipefail

ID="io.github.mcurtis.wispr-flow"
HOLD=3
PROCESSING=1.5
CYCLES=1
GAP=2

usage() {
  cat <<USAGE
Usage: scripts/simulate.sh [options]

Walks initializing -> listening -> stopping -> processing -> idle through a
fake launcher log while the plugin is enabled in the running shell.

Options:
  -l, --hold SECONDS         time spent listening (default $HOLD)
  -p, --processing SECONDS   time spent transcribing (default $PROCESSING;
                             the helper gives up on processing after 60)
  -n, --cycles N             number of dictations (default $CYCLES)
  -g, --gap SECONDS          idle time between dictations (default $GAP)
  -h, --help                 show this help

Examples:
  scripts/simulate.sh                    one quick dictation
  scripts/simulate.sh --hold 30          hold listening for a screenshot
  scripts/simulate.sh --hold 0.5 --processing 20
USAGE
}

fail() {
  echo "simulate: $*" >&2
  exit 1
}

while (( $# > 0 )); do
  case $1 in
    -l | --hold) HOLD=${2:?}; shift 2 ;;
    -p | --processing) PROCESSING=${2:?}; shift 2 ;;
    -n | --cycles) CYCLES=${2:?}; shift 2 ;;
    -g | --gap) GAP=${2:?}; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

command -v jq >/dev/null || fail "jq is required"
status() { omarchy-shell "$ID" status 2>/dev/null; }
status >/dev/null || fail "$ID is not running in the shell (enable it and put it on the bar first)"

CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json
# Raw JSON of the current value; null when the key is absent (the default).
original=$(jq -c --arg id "$ID" \
  'first(.bar.layout[]?[]? | select(.id == $id) | .logPath) // null' "$CONFIG")

workdir=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/wispr-flow-simulate.XXXXXX")
log=$workdir/launcher.log
: >"$log"

restore() {
  trap - EXIT INT TERM
  omarchy bar set "$ID" logPath "$original" --json >/dev/null || echo "simulate: could not restore logPath=$original" >&2
  rm -rf "$workdir"
}
trap restore EXIT
trap 'exit 130' INT TERM

# The service restarts its helper when logPath changes; wait until the new
# one follows the fake log, or its first lines would be missed.
wait_for_helper() {
  local deadline=$((SECONDS + 10))
  until status | jq -e --arg log "$log" '.helperLogPath == $log and .helperAvailable and .logPresent' >/dev/null 2>&1; do
    (( SECONDS < deadline )) || fail "the plugin did not pick up $log"
    sleep 0.1
  done
}

say() {
  printf '%s \xe2\x80\xba updateDictationStatus: %s { customAttributes: { uuid: '"'"'simulated'"'"' } }\n' \
    "$(date +%H:%M:%S.%3N)" "$1" >>"$log"
  echo "  $1"
}

omarchy bar set "$ID" logPath "$log" >/dev/null
wait_for_helper

for (( cycle = 1; cycle <= CYCLES; cycle++ )); do
  echo "dictation $cycle/$CYCLES"
  say initializing
  sleep 0.06
  say listening
  sleep "$HOLD"
  say stopping
  sleep 0.01
  say processing
  sleep "$PROCESSING"
  say idle
  if (( cycle < CYCLES )); then sleep "$GAP"; fi
done
sleep 0.5 # let the slide-out finish before the helper restarts
