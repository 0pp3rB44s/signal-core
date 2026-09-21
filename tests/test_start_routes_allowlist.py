"""Every route that can start `app.main` is known, and each carries the guard it depends on.

Agent C's finding was that ONE launcher (`start_bot.sh`) started the engine with none of the ceremony layers. Fixing that
one script is not enough if a new script (or an old one nobody looked at) can do the same. So the set of start routes
is pinned: a new route fails here until someone decides which guard it carries.

  * live routes   -> the four-layer ceremony (`launch_live.sh`) or the owner-token restore (`live_agent.sh`)
  * paper routes  -> environment that makes execution impossible (`launch_forward.sh`, `start_forward_paper.sh`)
  * `start_bot.sh`-> the app's own settings resolver refuses a live-capable environment (`app/launch_guard.py`)
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "tests", "docs", "logs", "state", "data_store", "data",
             "reports", "__pycache__", "archiving", "research", "Live_Release_v1", "RC2", "lsrl-v1-mirror"}
SCANNED = {".sh", ".py", ".template", ".plist", ".command"}
START_PATTERN = re.compile(r"-m\s+app\.main\b|\bapp/main\.py\b|from\s+app\.main\s+import|import\s+app\.main\b")

# route -> tokens that must all appear BEFORE the line that starts the engine, in this order
ROUTES = {
    "scripts/start_bot.sh": ["guard_assert_not_live_capable_env", "guard_assert_no_live_engine_running",
                             "guard_kill_own_app_main"],
    "scripts/launch_live.sh": ["guard_assert_critical_risks_cleared", "guard_assert_authorization_binding",
                               "guard_load_env \".env.live\"", "guard_assert_live_mode", "guard_assert_pilot_limits",
                               "\"$TYPED\" = \"$CONFIRM_PHRASE\""],
    "deploy/launchd/live_agent.sh": ["state/live.stop", "state/LIVE_PILOT_AUTHORISATION", "state/live_runtime.state",
                                     "AUTHORISED_COMMIT", "guard_assert_live_mode", "guard_assert_pilot_limits"],
    "scripts/launch_forward.sh": ["guard_load_env \".env.forward\"", "guard_assert_forward_mode"],
    "scripts/start_forward_paper.sh": ["export FORWARD_PAPER_ONLY=true", "export EXECUTION_ENABLED=false",
                                       "export EXECUTION_MODE=DRY_RUN", "export BITGET_API_KEY=\"\"",
                                       "export BITGET_API_SECRET=\"\"", "export BITGET_API_PASSPHRASE=\"\""],
}
# scripts that START one of the routes above (never app.main directly) -> the routes they may start
DELEGATORS = {
    "scripts/run_supervised.sh": {"start_bot.sh"},
    "scripts/forward_paper_keepalive.sh": {"launch_forward.sh"},
    "deploy/launchd/forward_agent.sh": {"forward_paper_keepalive.sh"},
    "validation_72h/supervise.sh": {"forward_paper_keepalive.sh"},
    "scripts/launch_live.sh": {"install_live_agent.sh"},
}
ROUTE_NAMES = ("start_bot.sh", "launch_live.sh", "launch_forward.sh", "start_forward_paper.sh", "live_agent.sh",
               "forward_paper_keepalive.sh", "forward_agent.sh", "run_supervised.sh")


def _files():
    for path in REPO.rglob("*"):
        if path.suffix in SCANNED and path.is_file() and not (set(path.relative_to(REPO).parts) & SKIP_DIRS):
            yield path


def _code_lines(text: str):
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)   # XML prose
    for number, line in enumerate(text.splitlines()):
        if not line.lstrip().startswith("#"):
            yield number, line


def _starts_engine(line: str) -> bool:
    """The line RUNS the engine; `pkill/pgrep/grep ... app.main` only names it."""
    return bool(START_PATTERN.search(line)) and not re.search(r"\b(pkill|pgrep|grep)\b", line)


def test_the_set_of_scripts_that_can_start_app_main_is_exactly_the_known_routes():
    found = set()
    for path in _files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if any(_starts_engine(line) for _, line in _code_lines(text)):
            found.add(str(path.relative_to(REPO)))
    # app/main.py names itself in its own logger; those are entries, not launchers.
    found -= {"app/main.py", "app/launch_guard.py", "app/runner.py", "dashboard_v2/bot_control.py",
              "dashboard_v3/panels/runtime.py"}
    assert found == set(ROUTES), (
        f"unknown start route(s): {sorted(found - set(ROUTES))}; vanished route(s): {sorted(set(ROUTES) - found)}. "
        "A new way to start the engine must be added to ROUTES with the guard it carries.")


def test_every_route_runs_its_guards_before_the_line_that_starts_the_engine():
    for route, tokens in ROUTES.items():
        lines = [line for _, line in _code_lines((REPO / route).read_text())]
        starts = [i for i, line in enumerate(lines) if _starts_engine(line)]
        assert starts, f"{route}: no start line found"
        first = starts[0]
        last = -1
        for token in tokens:
            hits = [i for i, line in enumerate(lines[:first]) if token in line]
            assert hits, f"{route}: guard `{token}` does not appear before the engine is started"
            assert min(hits) > last, f"{route}: guard `{token}` runs out of order (must follow the previous guard)"
            last = min(hits)


def _invokes(line: str, name: str) -> bool:
    """A shell line whose COMMAND is `name` (not a message that merely mentions it)."""
    return bool(re.match(rf"^\s*(if\s+|!\s*|exec\s+|nohup\s+|\$\(\s*)*((bash|sh)\s+)?(\./)?\S*/{re.escape(name)}(\s|$|\"|\))", line))


def test_only_the_known_delegators_invoke_a_start_route():
    allowed = set(DELEGATORS) | set(ROUTES)
    for path in _files():
        rel = str(path.relative_to(REPO))
        if path.suffix not in {".sh", ".template", ".plist"} or rel in allowed:
            continue
        for _, line in _code_lines(path.read_text()):
            for name in ROUTE_NAMES:
                assert not _invokes(line, name), f"{rel} invokes {name} but is not a known delegator: {line.strip()}"


def test_no_python_module_shells_out_to_a_start_route():
    import ast
    exact = re.compile(rf"^(bash\s+|sh\s+)?(\./)?(scripts|deploy/launchd)/({'|'.join(map(re.escape, ROUTE_NAMES))})(\s.*)?$")
    for path in _files():
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and exact.match(node.value.strip()):
                raise AssertionError(f"{path.relative_to(REPO)}:{node.lineno} builds a call to a start route: {node.value!r}")


def test_delegators_only_start_the_routes_they_are_documented_to():
    for script, names in DELEGATORS.items():
        if not (REPO / script).exists():                   # a partial checkout; the route set test still pins the rest
            continue
        lines = [line for _, line in _code_lines((REPO / script).read_text())]
        invoked = {n for n in ROUTE_NAMES + ("install_live_agent.sh",) if any(_invokes(line, n) for line in lines)}
        assert invoked == names, f"{script}: starts {sorted(invoked)}, documented {sorted(names)}"


def test_the_paper_routes_can_never_read_the_live_environment_file():
    for route in ("scripts/launch_forward.sh", "scripts/start_forward_paper.sh"):
        text = "\n".join(line for _, line in _code_lines((REPO / route).read_text()))
        assert ".env.live" not in text and "LIVE_PILOT_AUTHORISATION" not in text, route


def test_the_forward_launchers_do_not_use_the_live_agent_and_the_live_agent_never_reads_forward_settings():
    live = "\n".join(line for _, line in _code_lines((REPO / "deploy/launchd/live_agent.sh").read_text()))
    assert ".env.forward" not in live and "guard_assert_forward_mode" not in live


def test_the_notify_only_launchd_job_cannot_start_the_engine():
    plist = (REPO / "scripts/com.cgc.tradingbot.plist.template").read_text()
    program = plist[plist.index("<key>ProgramArguments</key>"):plist.index("</array>", plist.index("<key>ProgramArguments</key>"))]
    assert "nohup" not in program and "-m app.main" not in program and "python" not in program
    assert "pgrep" in program and "osascript" in program


def test_stop_all_stops_and_never_starts():
    text = "\n".join(line for _, line in _code_lines((REPO / "scripts/stop_all.sh").read_text()))
    assert "nohup" not in text and "-m app.main" not in text.replace('pkill -f "python3 -u -m app.main"', "") \
        .replace('pkill -f "python3 -m app.main"', "")
    assert "touch state/live.stop" in text                       # a deliberate stop must survive a reboot
