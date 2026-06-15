from __future__ import annotations

import os
import subprocess
from pathlib import Path

from flask import Flask, jsonify, render_template

from app.config import get_settings
from dashboard.data_provider import load_dashboard_data


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="../dashboard/templates",
        static_folder="../dashboard/static",
    )

    base_dir = Path(__file__).resolve().parents[1]
    scripts_dir = base_dir / "scripts"

    def _run_script(name: str) -> tuple[bool, str]:
        script_path = scripts_dir / name
        try:
            subprocess.Popen(["bash", str(script_path)], cwd=str(base_dir))
            return True, f"started {name}"
        except Exception as exc:
            return False, str(exc)

    def _set_env_value(key: str, value: str) -> tuple[bool, str]:
        env_path = base_dir / ".env"
        try:
            lines = env_path.read_text().splitlines()
            updated = False
            new_lines = []
            for line in lines:
                if line.startswith(f"{key}="):
                    new_lines.append(f"{key}={value}")
                    updated = True
                else:
                    new_lines.append(line)
            if not updated:
                new_lines.append(f"{key}={value}")
            env_path.write_text("\n".join(new_lines) + "\n")
            os.environ[key] = value
            if hasattr(get_settings, "cache_clear"):
                get_settings.cache_clear()
            return True, f"set {key}={value}"
        except Exception as exc:
            return False, str(exc)

    def _safe_stop_and_disable_execution() -> tuple[bool, str]:
        ok_env, msg_env = _set_env_value("EXECUTION_ENABLED", "false")
        _run_script("stop_all.sh")
        return ok_env, f"KILL SWITCH ACTIVE | {msg_env} | stopped bot/dashboard"

    @app.get("/")
    def index():
        if hasattr(get_settings, "cache_clear"):
            get_settings.cache_clear()
        data = load_dashboard_data()
        settings = get_settings()
        return render_template("index.html", data=data, settings=settings)

    @app.get("/api/dashboard")
    def api_dashboard():
        return jsonify(load_dashboard_data())

    @app.post("/api/control/start_bot")
    def start_bot():
        ok, msg = _run_script("start_bot.sh")
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/control/start_dashboard")
    def start_dashboard():
        ok, msg = _run_script("start_dashboard.sh")
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/control/stop_all")
    def stop_all():
        ok, msg = _run_script("stop_all.sh")
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/control/restart_bot")
    def restart_bot():
        bot_pid_path = base_dir / "state" / "bot.pid"
        try:
            if bot_pid_path.exists():
                pid = bot_pid_path.read_text().strip()
                if pid:
                    subprocess.run(["kill", pid], cwd=str(base_dir), capture_output=True, text=True)
                bot_pid_path.unlink(missing_ok=True)
        except Exception:
            pass

        ok, msg = _run_script("start_bot.sh")
        return jsonify({"ok": ok, "message": f"restart bot only: {msg}"})

    @app.post("/api/control/execution_off")
    def execution_off():
        ok, msg = _set_env_value("EXECUTION_ENABLED", "false")
        return jsonify({"ok": ok, "message": msg})

    @app.post("/api/control/execution_on_dryrun")
    def execution_on_dryrun():
        ok_mode, msg_mode = _set_env_value("EXECUTION_MODE", "DRY_RUN")
        ok_exec, msg_exec = _set_env_value("EXECUTION_ENABLED", "true")
        ok_confirm, msg_confirm = _set_env_value("EXECUTION_REQUIRE_CONFIRMATION", "true")
        return jsonify({
            "ok": ok_mode and ok_exec and ok_confirm,
            "message": f"SAFE EXECUTION ON | {msg_mode}; {msg_exec}; {msg_confirm}",
        })

    @app.post("/api/control/dry_run_mode")
    def dry_run_mode():
        ok_mode, msg_mode = _set_env_value("EXECUTION_MODE", "DRY_RUN")
        ok_exec, msg_exec = _set_env_value("EXECUTION_ENABLED", "false")
        return jsonify({"ok": ok_mode and ok_exec, "message": f"{msg_mode}; {msg_exec}"})

    @app.post("/api/control/kill_switch")
    def kill_switch():
        ok, msg = _safe_stop_and_disable_execution()
        return jsonify({"ok": ok, "message": msg})

    return app


def main() -> None:
    settings = get_settings()
    app = create_app()
    app.run(
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        debug=settings.dashboard_debug,
    )


if __name__ == "__main__":
    main()
