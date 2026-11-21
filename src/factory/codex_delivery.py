from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
from shlex import quote
from tempfile import TemporaryDirectory
from platform import machine, system
from time import time

from factory.agent_profile import CLI_BUILD
from factory.codex_runner import run_cli
from factory.codex_transport import KEY_NAME, Proxy, proxy_model
from factory.codex_workspace import Workspace
from factory.evidence import Conflict, Evidence, encode
from factory.harbor_binding import Binding


CHATGPT_URL = "https://chatgpt.com/backend-api"
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


def preflight(cli: str, env: dict, cwd: Path, profile: dict, transport: dict) -> str | None:
    def output(*args):
        return subprocess.run(
            [cli, *args], cwd=cwd, env=env, timeout=15, capture_output=True, text=True,
        )

    version = output("--version")
    if version.returncode or version.stdout.strip() != CLI_BUILD:
        return "cli_build_mismatch"
    if transport.get("kind") == "cli-proxy":
        return proxy_model(transport, env, profile["model"])
    login = output("login", "status")
    if login.returncode or "Logged in using ChatGPT" not in login.stdout + login.stderr:
        return "login_required"
    catalog = output(
        "debug", "models", "-c", "model_provider=openai", "-c", "forced_login_method=chatgpt",
        "-c", "features.remote_models=true", "-c", "chatgpt_base_url=" + json.dumps(CHATGPT_URL),
    )
    try:
        models = json.loads(catalog.stdout)["models"] if catalog.returncode == 0 else ()
    except (ValueError, KeyError):
        return "model_catalog_unavailable"
    if not any(model["slug"] == profile["model"] and model["visibility"] == "list" for model in models):
        return "model_unavailable"
    return None


def cli_args(cli: str, cwd: Path, store: Evidence, request: dict, workspace: Workspace | Binding, profile: dict) -> list[str]:
    mcp_args = [
        "-m", "factory.codex_bridge", str(store.root), request["run_id"], workspace.name, request["base_sha"],
    ]
    if isinstance(workspace, Binding):
        mcp_args.append(workspace.rollout)
    source = str(Path(__file__).resolve().parents[1])
    settings = {
        "model_reasoning_effort": profile["reasoning_effort"],
        "approval_policy": "never",
        "default_permissions": "factory",
        "shell_environment_policy.inherit": "none",
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
        "mcp_servers.factory.enabled_tools": ["workspace", "milestone"],
        "mcp_servers.factory.tools.workspace.approval_mode": "approve",
        "mcp_servers.factory.tools.milestone.approval_mode": "approve",
        "mcp_servers.factory.tool_timeout_sec": 58,
    }
    transport = request.get("transport", {})
    if transport.get("kind") == "cli-proxy":
        settings.update({
            "model_provider": "factory-hub",
            "model_providers.factory-hub.name": "Factory CLI Proxy",
            "model_providers.factory-hub.base_url": transport["url"],
            "model_providers.factory-hub.wire_api": "responses",
            "model_providers.factory-hub.env_key": KEY_NAME,
            "model_providers.factory-hub.requires_openai_auth": False,
        })
    else:
        settings.update({
            "forced_login_method": "chatgpt", "model_provider": "openai",
            "chatgpt_base_url": CHATGPT_URL, "features.remote_models": True,
        })
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
    proof = ()
    if request.get("workspace_marker"):
        proof = (f"Before editing, use the workspace shell to write the literal string {request['run_id']} "
                 "to .git/factory-marker and read it back. This is a delivery isolation check.",)
    if request.get("evaluation"):
        proof += ("The trusted bridge publishes the bundle to /output after the submission milestone.",)
    return "\n\n".join((
        profile["profile"]["instructions"],
        request["outcome"],
        "Use the factory MCP workspace tool for every product command. The local CLI directory is empty. "
        "Use the factory milestone tool in this order: investigation, red, green, refactor, submission. "
        "Commit red tests, then green code, then any refactor with Git inside the workspace. "
        "At red provide the test argv. The runner uses exactly that test for green and submission. "
        "The final response should report the submitted SHA. Do not push, request release or edit grading.",
        f"Resuming from recorded phases: {recorded}.",
        *proof, *skills,
    )).encode()


def dispatch(
    store: Evidence, request: dict, ticket: dict, cli: str, auth_home: Path, proxy: Proxy | None = None,
    *, binding: Binding | None = None, sentinel: Path | None = None,
) -> dict:
    run_id, attempt = request["run_id"], ticket["attempt"]
    key = f"attempt-{attempt}"
    if bool(request.get("evaluation")) != (binding is not None):
        raise Conflict("evaluation requires its admitted workspace binding")
    if binding is not None and (binding.name != run_id or binding.root != store.root
                                or request.get("evaluation", {}).get("rollout") != binding.rollout):
        raise Conflict("workspace binding differs from the admitted rollout")
    with store.lock(run_id if binding is not None else "codex-runner"):
        if (result := store.read(run_id, key)) is not None:
            return result
        marker_root = store.run_dir(run_id) if binding is not None else store.root
        marker = marker_root / "active-codex-run.json"
        reference = request["configuration"]
        result = {"attempt": attempt, "configuration": reference, "billing": None, "usage": None}
        try:
            profile = json.loads(store.read_blob(reference))
            text = prompt(store, request, profile)
            store.read_blob(profile["source_digest"])
            store.read_blob(request["base_checkpoint"])
        except (FileNotFoundError, ValueError, KeyError):
            return store.record(run_id, key, {**result, "reason": "pinned_configuration_unavailable"})
        if marker.exists() or store.read(run_id, f"{key}-intent") is not None:
            return store.record(run_id, key, {**result, "reason": "unfenced_prior_attempt"})
        if time() >= request["deadline_epoch"]:
            return store.record(run_id, key, {**result, "reason": "deadline_exceeded"})
        if store.read(run_id, "submission") is not None:
            return store.record(run_id, key, {**result, "reason": "completed"})
        with TemporaryDirectory(prefix="factory-codex-") as directory:
            temporary = Path(directory).resolve()
            cwd = temporary / "workspace"
            cwd.mkdir()
            transport = request.get("transport", {})
            if transport.get("kind") == "cli-proxy":
                if proxy is None:
                    return store.record(run_id, key, {**result, "reason": "proxy_configuration_missing"})
                codex_home = temporary / "codex"
                codex_home.mkdir(mode=0o700)
                env = cli_environment(codex_home)
                env.update(proxy.environment(store, transport, temporary))
            else:
                env = cli_environment(auth_home)
            if reason := preflight(cli, env, cwd, profile, transport):
                return store.record(run_id, key, {**result, "reason": reason})
            snapshot_ref = request["base_checkpoint"]
            for phase in ("green", "red"):
                if (milestone := store.read(run_id, phase)) is not None:
                    snapshot_ref = milestone["checkpoint"]
                    break
            snapshot = json.loads(store.read_blob(snapshot_ref))
            workspace = binding if binding is not None else Workspace(f"factory-codex-{run_id}-{attempt}")
            events = store.run_dir(run_id) / f"{key}.jsonl"
            intent = {"attempt": attempt, "workspace": workspace.name, "checkpoint": snapshot_ref,
                      "configuration": reference, "deadline_epoch": request["deadline_epoch"],
                      "runner_platform": f"{system()}/{machine()}", "runner_python": sys.version.split()[0],
                      "isolation": {"host_native_write": False, "host_tool_network": False,
                                    "workspace_network": "none", "workspace_mounts": [],
                                    "local_tool_cwd": "empty", "approval_policy": "never"}}
            store.record(run_id, f"{key}-intent", intent)
            with marker.open("xb") as stream:
                stream.write(encode({"run_id": run_id, **intent}))
                stream.flush()
                os.fsync(stream.fileno())
            scope = nullcontext() if binding is not None else workspace.provision(request["workspace_image"], snapshot)
            sentinel_check = f"test ! -e {quote(str(sentinel))} && " if sentinel else ""
            with scope:
                probe = workspace.run([
                    "sh", "-ec", 'test "$PWD" = /work/product && test -d .git && '
                    'test ! -e /var/run/docker.sock && test ! -e /evidence && test ! -e /grading && '
                    'test ! -e "$HOME/.codex/auth.json" && test -z "${OPENAI_API_KEY-}${GH_TOKEN-}" && '
                    'test ! -e /Users && ' + sentinel_check + 'printf %s "$PWD"',
                ])
                if probe["exit_code"]:
                    raise Conflict("workspace isolation probe failed")
                store.record(run_id, f"{key}-workspace", probe)
                result.update(run_cli(
                    cli_args(cli, cwd, store, request, workspace, profile),
                    cwd, env, text, events, request["deadline_epoch"],
                    **({"cancelled": lambda: not binding.active()} if binding is not None else {}),
                ))
                if result["reason"] == "completed" and request.get("workspace_marker"):
                    marker_probe = workspace.run(["cat", ".git/factory-marker"])
                    marker_found = (not marker_probe["exit_code"] and not marker_probe["truncated"]
                                    and marker_probe["output"].strip() == run_id)
                    store.record(run_id, f"{key}-marker", {"assigned_workspace": marker_found, "marker": run_id})
                    if not marker_found:
                        result["reason"] = "workspace_marker_missing"
            result["native_events"] = store.blob(events.read_bytes())
            if result["reason"] == "completed" and store.read(run_id, "submission") is None:
                result["reason"] = "missing_submission"
            marker.unlink()
        return store.record(run_id, key, result)
