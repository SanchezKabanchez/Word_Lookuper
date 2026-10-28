#!/usr/bin/env bash
set -euo pipefail
cd /workspace/Word_Lookuper
tools_dir=/workspace/.word-lookup-tools
export PATH="$tools_dir/root/usr/bin:$PATH"
test_config=$(mktemp -d /tmp/word-lookup-tests.XXXXXX)
display_file=$(mktemp /tmp/word-lookup-display.XXXXXX)
xvfb_pid=''
cleanup() {
    if [[ -n "$xvfb_pid" ]]; then kill "$xvfb_pid" 2>/dev/null || true; wait "$xvfb_pid" 2>/dev/null || true; fi
    rm -rf "$test_config"
    rm -f "$display_file"
}
trap cleanup EXIT
if [[ -z "${DISPLAY:-}" ]]; then
    Xvfb -displayfd 3 -screen 0 1280x800x24 -nolisten tcp 3>"$display_file" >"$test_config/xvfb.log" 2>&1 &
    xvfb_pid=$!
    for attempt in {1..50}; do
        [[ -s "$display_file" ]] && break
        kill -0 "$xvfb_pid" 2>/dev/null || { cat "$test_config/xvfb.log"; exit 1; }
        sleep 0.1
    done
    [[ -s "$display_file" ]] || { cat "$test_config/xvfb.log"; exit 1; }
    export DISPLAY=":$(cat "$display_file")"
fi
export XDG_CONFIG_HOME="$test_config"
.venv/bin/python -m pytest "$@"
