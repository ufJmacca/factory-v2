import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import time

from factory.agent_profile import CLI_BUILD
from factory.codex_runner import run_cli
from factory.codex_workspace import Workspace
from factory.evidence import Conflict, Evidence, digest, encode


DISABLED = (
    "shell_tool", "unified_exec", "workspace_dependencies", "view_image", "apps",
    "plugins", "browser_use", "browser_use_external", "computer_use", "image_generation",
    "multi_agent", "multi_agent_v2", "hooks", "memories", "goals", "js_repl",
    "request_permissions_tool", "unbounded_connection_retries", "daemon_auto_start",
)


def cli_environment(auth_home: Path) -> dict:
    return {
        "HOME": str(Path.home()), "CODEX_HOME": str(auth_home),
        "PATH": os.defpath + ":/opt/homebrew/bin:/usr/local/bin",
        "LANG": "en_US.UTF-8",
    }


def preflight(cli: str, env: dict, cwd: Path) -> str | None:
    def output(*args):
        return subprocess.run(
            [cli, *args], cwd=cwd, env=env, timeout=15, capture_output=True, text=True,
        )

    version = output("--version")
    if version.returncode or version.stdout.strip() != CLI_BUILD:
        return "cli_build_mismatch"
    login = output("login", "status")
    if login.returncode or "Logged in using ChatGPT" not in login.stdout + login.stderr:
        return "login_required"
    return None


def cli_args(cli: str, cwd: Path, store: Evidence, request: dict, workspace: Workspace, profile: dict) -> list[str]:
    mcp_args = [
        "-m", "factory.codex_bridge", str(store.root), request["run_id"], workspace.name, request["base_sha"],
    ]
    source = str(Path(__file__).resolve().parents[1])
    settings = {
        "forced_login_method": "chatgpt",
        "model_provider": "openai",
        "model_reasoning_effort": profile["reasoning_effort"],
        "approval_policy": "never",
        "default_permissions": "factory",
        "permissions.factory.network.enabled": False,
        "web_search": "disabled",
        "skills.bundled.enabled": False,
        "skills.include_instructions": False,
        "features.skip_host_skill_discovery": True,
        "mcp_servers.factory.command": sys.executable,
        "mcp_servers.factory.args": mcp_args,
        "mcp_servers.factory.env.PYTHONPATH": source,
        "mcp_servers.factory.env.PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "mcp_servers.factory.required": True,
        "mcp_servers.factory.tool_timeout_sec": 58,
    }
    overrides = [item for key, value in settings.items() for item in ("-c", f"{key}={json.dumps(value)}")]
    overrides += ["-c", 'permissions.factory.filesystem={":minimal"="read",":workspace_roots"="read"}']
    flags = [item for feature in DISABLED for item in ("--disable", feature)]
    return [
        cli, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
        "--ignore-rules", "--strict-config", "-C", str(cwd), "--model", profile["model"],
        *overrides, *flags, "-",
    ]


def prompt(store: Evidence, request: dict, profile: dict) -> bytes:
    skills = [
        f"--- {name} ---\n{store.read_blob(reference).decode()}"
        for name, reference in profile["skills"].items()
    ]
    recorded = [phase for phase in ("investigation", "red", "green", "refactor")
                if store.read(request["run_id"], phase) is not None]
    return "\n\n".join((
        profile["profile"]["instructions"],
        request["outcome"],
        "Use the factory MCP workspace tool for every product command. The local CLI directory is empty. "
        "Use the factory milestone tool in this order: investigation, red, green, refactor, submission. "
        "Commit red tests, then green code, then any refactor with Git inside the workspace. "
        "At red provide the test argv. The runner uses exactly that test for green and submission. "
        "The final response should report the submitted SHA. Do not push, request release or edit grading.",
        f"Resuming from recorded phases: {recorded}.",
        *skills,
    )).encode()


def dispatch(store: Evidence, request: dict, ticket: dict, cli: str, auth_home: Path) -> dict:
    run_id, attempt = request["run_id"], ticket["attempt"]
    key = f"attempt-{attempt}"
    with store.lock("codex-runner"):
        if (result := store.read(run_id, key)) is not None:
            return result
        marker = store.root / "active-codex-run.json"
        reference = request["configuration"]
        profile = json.loads(store.read_blob(reference))
        text = prompt(store, request, profile)
        for resource in (profile["source_digest"], request["base_checkpoint"]):
            store.read_blob(resource)
        result = {"attempt": attempt, "configuration": reference, "billing": None, "usage": None}
        if marker.exists() or store.read(run_id, f"{key}-intent") is not None:
            return store.record(run_id, key, {**result, "reason": "unfenced_prior_attempt"})
        if time() >= request["deadline_epoch"]:
            return store.record(run_id, key, {**result, "reason": "deadline_exceeded"})
        if store.read(run_id, "submission") is not None:
            return store.record(run_id, key, {**result, "reason": "completed"})
        with TemporaryDirectory(prefix="factory-codex-") as directory:
            cwd = Path(directory).resolve()
            env = cli_environment(auth_home)
            if reason := preflight(cli, env, cwd):
                return store.record(run_id, key, {**result, "reason": reason})
            snapshot_ref = request["base_checkpoint"]
            for phase in ("green", "red"):
                if (milestone := store.read(run_id, phase)) is not None:
                    snapshot_ref = milestone["checkpoint"]
                    break
            snapshot = json.loads(store.read_blob(snapshot_ref))
            workspace = Workspace(f"factory-codex-{run_id}-{attempt}")
            events = store.run_dir(run_id) / f"{key}.jsonl"
            intent = {"attempt": attempt, "workspace": workspace.name, "checkpoint": snapshot_ref,
                      "configuration": reference, "deadline_epoch": request["deadline_epoch"]}
            store.record(run_id, f"{key}-intent", intent)
            with marker.open("xb") as stream:
                stream.write(encode({"run_id": run_id, **intent}))
                stream.flush()
                os.fsync(stream.fileno())
            with workspace.provision(request["workspace_image"], snapshot):
                probe = workspace.run([
                    "sh", "-ec", 'test "$PWD" = /work/product && test -d .git && '
                    'test ! -e /var/run/docker.sock && test ! -e /evidence && test ! -e /grading && '
                    'test ! -e "$HOME/.codex/auth.json" && test -z "${OPENAI_API_KEY-}${GH_TOKEN-}" && '
                    'test ! -e /Users && printf %s "$PWD"',
                ])
                if probe["exit_code"]:
                    raise Conflict("workspace isolation probe failed")
                store.record(run_id, f"{key}-workspace", probe)
                result.update(run_cli(
                    cli_args(cli, cwd, store, request, workspace, profile),
                    cwd, env, text, events, request["deadline_epoch"],
                ))
            result["native_events"] = store.blob(events.read_bytes())
            if result["reason"] == "completed" and store.read(run_id, "submission") is None:
                result["reason"] = "missing_submission"
            marker.unlink()
        return store.record(run_id, key, result)
