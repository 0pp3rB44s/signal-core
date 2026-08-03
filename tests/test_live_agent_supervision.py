"""Static contract for adopting the engine started by launch_live.sh."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_existing_authorised_engine_is_adopted_not_successfully_ignored():
    source = (REPO / "deploy/launchd/live_agent.sh").read_text(encoding="utf-8")
    adoption = source.index("adopting already-running authorised engine")
    restart = source.index("adopted engine exited; agent returning non-zero")
    assert 'BOT_PID="$(pgrep -f' in source[:adoption]
    assert 'while ps -p "$BOT_PID"' in source[adoption:restart]
    assert "exit 1" in source[restart:]
    assert "engine already running; nothing to do" not in source
