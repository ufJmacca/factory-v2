"""Trusted-host driver for the pinned CLI during the bridge verification."""
import argparse
from contextlib import ExitStack, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
from shlex import quote
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time
from unittest.mock import patch

from factory import codex_delivery
from factory.agent_profile import resolve
from factory.checkpoint import encode as base64_encode, git
from factory.codex_transport import KEY_NAME, Proxy
from factory.codex_workspace import seed
from factory.evidence import Conflict, Evidence, encode
from factory.harbor_binding import Binding, TRUSTED_WORKSPACE_MARKER
from factory.harbor_receipt import export_submission, runtime_pin
from factory.live_codex import grade
from scripts import probe_codex_cli as fixture_cli


REPO = Path.cwd()
GRADER = REPO / "grading/check_verified_patch.py"


def proxy_from_env():
    return Proxy(os.environ["BRIDGE_HUB_URL"], Path(os.environ["BRIDGE_HUB_KEY_FILE"]),
                 Path(os.environ["BRIDGE_HUB_CA_FILE"]))


def prepare(store, sha, image, worker, live):
    frozen = store.read("bridge", "frozen", required=True)
    if os.environ.get("BRIDGE_RESUME_ROOT"):
        prior = store.read("bridge", "common", required=True)
        if not live or worker != prior["runtime"]["worker_image"] or proxy_from_env().pin(store) != prior["transport"]:
            raise Conflict("resume requires the original worker image and CLI Proxy Hub")
        return
    profile = resolve(REPO, sha, store)
    transport = proxy_from_env().pin(store) if live else {"kind": "scripted-responses"}
    if live:
        if transport != frozen["historical_transport"]:
            raise Conflict("live verification requires the frozen Phase 03 CLI Proxy Hub route")
        store.record("bridge", "approval", {"approved_by": os.environ["BRIDGE_APPROVED_BY"],
                                          "max_executions": 2, "max_minutes_each": 10, "max_attempts_each": 1})
    store.record("bridge", "common", {
        "schema_version": 1, "factory_sha": sha, "max_attempts": 1, "workspace_marker": True,
        "base_sha": frozen["base_sha"], "workspace_image": image,
        "base_checkpoint": store.blob(encode(seed(REPO, frozen["base_sha"]))),
        "configuration": store.blob(encode(profile)), "transport": transport,
        "runtime": runtime_pin(profile, sha, worker, transport),
        "product_grader": store.blob(GRADER.read_bytes()),
    })


def calls_for(store, request):
    base = request["base_sha"]
    task = request.get("evaluation", {}).get("task_id", "verified-patch")
    historical = task == "verified-patch"
    bundle = (store.root / "calibration/known-solution.bundle" if historical
              else store.root / "controls" / task / "known.bundle")
    with TemporaryDirectory() as directory:
        checkout = Path(directory)
        git(checkout, "init")
        git(checkout, "fetch", str(bundle), "refs/heads/submission")
        patch_bytes = git(checkout, "diff", "--binary", "--no-renames", base, "FETCH_HEAD", "--", ".", ":(exclude)tests")
    encoded = base64_encode(patch_bytes)
    marker = request["run_id"]
    ready = store.read(marker, "harbor-ready")
    hold = " && sleep 20" if ready and "task_id" not in request["evaluation"] else ""
    test = "import importlib.util\nassert importlib.util.find_spec('factory.verified_patch')\n"
    if not historical:
        path = {"profile-preview": "src/factory/agent_profile.py",
                "rpc-unknown-operation": "src/factory/local_rpc.py"}[task]
        with TemporaryDirectory() as directory:
            checkout = Path(directory)
            git(checkout, "init")
            git(checkout, "fetch", str(bundle), "refs/heads/submission")
            expected = sha256(git(checkout, "show", f"FETCH_HEAD:{path}")).hexdigest()
        test = ("from hashlib import sha256\nfrom pathlib import Path\n"
                f"assert sha256(Path({path!r}).read_bytes()).hexdigest() == {expected!r}\n")
    verbose = (
        "import sys; from pathlib import Path; "
        "sys.stdout.write('x' * 1_000_000); sys.stdout.flush(); "
        "Path('.git/factory-verbose-probe').touch(); sys.exit(7)"
    )
    initial = "pwd"
    if request.get("workspace_marker") != TRUSTED_WORKSPACE_MARKER:
        initial = (f"test ! -e .git/factory-marker && printf %s {quote(marker)} > .git/factory-marker "
                   f"&& test \"$(cat .git/factory-marker)\" = {quote(marker)}{hold} && printf %s {quote(marker)}")
    tools = [
        ("workspace", {"command": initial}),
        ("workspace", {"command": f"python -c {quote(verbose)}"}),
        ("workspace", {"command": "test -f .git/factory-verbose-probe"}),
        ("milestone", {"phase": "investigation", "note": "Verified workspace placement."}),
        ("workspace", {"command": f"printf %s {quote(test)} > tests/bridge_red.py && "
                                  f"git add tests/bridge_red.py && {fixture_cli.COMMIT} -m 'Add behavior test'"}),
        ("milestone", {"phase": "red", "note": "The module is missing on the base.",
                       "argv": ["python", "-IB", "-c", "import sys; sys.path.insert(0,'src'); exec(open('tests/bridge_red.py').read())"]}),
    ]
    if request.get("evaluation", {}).get("scripted_rejection"):
        tools.insert(0, ("workspace", {"command": "git status --short", "extra": 1}))
    for offset in range(0, len(encoded), 4000):
        tools.append(("workspace", {"command": f"printf %s {quote(encoded[offset:offset + 4000])} >> /tmp/bridge.patch64"}))
    tools.extend([
        ("workspace", {"command": "base64 -d /tmp/bridge.patch64 > /tmp/bridge.patch && git apply --index /tmp/bridge.patch "
                                  f"&& {fixture_cli.COMMIT} -m 'Replay known fixture solution'"}),
        ("milestone", {"phase": "green", "note": "The same test now finds the command module."}),
        ("milestone", {"phase": "refactor", "note": "The scripted case uses the frozen fixture's known solution."}),
        ("milestone", {"phase": "submission", "note": "The committed revision is ready for the separate grader."}),
    ])
    return tools


@contextmanager
def scripted_cli(store, request):
    fixture_cli.CALLS = calls_for(store, request)
    real_args = codex_delivery.cli_args
    real_environment = codex_delivery.cli_environment
    with fixture_cli.endpoint() as url, TemporaryDirectory() as home, ExitStack() as stack:
        stack.enter_context(patch.object(codex_delivery, "preflight", return_value=None))
        stack.enter_context(patch.object(codex_delivery, "cli_environment",
                                         side_effect=lambda path: {**real_environment(path), KEY_NAME: "test-key"}))
        stack.enter_context(patch.object(codex_delivery, "cli_args", side_effect=lambda cli, cwd, store, req, ws, profile:
                                         real_args(cli, cwd, store, {**req, "transport": {"kind": "cli-proxy", "url": url}},
                                                   ws, profile)))
        yield Path(home)


def run_one(store, run_id, cli, sentinel, records):
    request = store.read(run_id, "request", required=True)
    spec = request.get("evaluation")
    binding = Binding(store.root, spec["rollout"]) if spec else None
    ticket = store.read(run_id, "dispatch-attempt-1", required=True)
    live = request["transport"]["kind"] == "cli-proxy"
    if live and os.environ.get("FACTORY_HARBOR_SCRIPTED_ONLY"):
        raise Conflict("scripted Harbor runner received a live transport")

    if binding:
        binding.require_active()
    try:
        if live:
            result = codex_delivery.dispatch(store, request, ticket, cli, Path.home() / ".codex",
                                             proxy_from_env(), binding=binding, sentinel=sentinel)
        else:
            with scripted_cli(store, request) as home:
                result = codex_delivery.dispatch(store, request, ticket, cli, home, binding=binding, sentinel=sentinel)
        if result["reason"] != "completed":
            return
        while time() < request["deadline_epoch"] + 20:
            if store.read(run_id, "dispatch-verify"):
                if binding:
                    export_submission(store, request, binding, result)
                else:
                    frozen = store.read("bridge", "frozen", required=True)
                    grade(store, request, GRADER.read_bytes(), records, frozen["fixture_run"])
                return
            sleep(0.2)
        raise TimeoutError("delivery did not park at submission")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if store.read(run_id, "attempt-1") is None:
            reason = "lease_lost" if binding and not binding.active() else "runner_infrastructure_failed"
            store.record(run_id, "attempt-1", {"attempt": 1, "reason": reason, "usage": None, "billing": None})
        if binding:
            binding.revoke("runner_failed")
        raise


def inspect_scope(store, run_id, proof, ident):
    instance = json.loads(subprocess.check_output(["docker", "inspect", ident]))[0]
    config = instance["HostConfig"]
    request = store.read(run_id, "request")
    spec = (request or {}).get("evaluation", {})
    if "task_id" in spec and (
        instance["Image"] != request["workspace_image"] or config["PidsLimit"] != 64
        or config["NanoCpus"] != 10**9 or config["Memory"] != 512 * 1024**2
    ):
        raise Conflict("trial image or resources differ from the frozen experiment")
    mounts = {mount["Destination"]: mount for mount in instance["Mounts"] if mount["Type"] != "tmpfs"}
    destinations = {path: row["RW"] for path, row in mounts.items()}
    if (config["NetworkMode"] != "none" or config["Privileged"] or not config["ReadonlyRootfs"]
            or destinations != {"/seed": False, "/output": True, "/logs/agent": True,
                                "/logs/verifier": False, "/logs/artifacts": False}
            or Path(mounts["/output"]["Source"]).name != proof["assignment"]):
        raise Conflict("trial container differs from the locked workspace boundary")
    store.record(run_id, "provider-start", {"container": ident, "offline": True, "readonly_root": True,
                                          "mounts": destinations})


def watch(store, cli, sentinel, records, server):
    children = {}
    resume_at = None
    do_pause = True
    stop = Path(os.environ["BRIDGE_WATCH_STOP"])
    try:
        while not stop.exists():
            if do_pause and store.read("bridge-lease-loss", "pause-owner"):
                subprocess.run(["docker", "pause", server], check=True, stdout=subprocess.DEVNULL)
                do_pause = False
                resume_at = monotonic() + 14
            if resume_at is not None and monotonic() >= resume_at:
                subprocess.run(["docker", "unpause", server], check=True, stdout=subprocess.DEVNULL)
                resume_at = None
                store.record("bridge-lease-loss", "owner-resumed", {"paused_seconds": 14})
            for path in (store.root / "runs").glob("eval-*/harbor-ready.json"):
                name = path.parent.name
                if store.read(name, "provider-stop"):
                    continue
                proof = store.read(name, "harbor-ready", required=True)
                binding = Binding(store.root, proof["rollout"])
                ident = proof["container"]
                if binding.active():
                    if store.read(name, "request") and not store.read(name, "provider-start"):
                        inspect_scope(store, name, proof, ident)
                    continue
                inventory = subprocess.check_output(["docker", "ps", "-aq", "--filter", f"id={ident}"], text=True)
                if not inventory.strip():
                    store.record(name, "provider-stop", {"container": ident, "removed": True})
            for path in (store.root / "runs").glob("*/dispatch-attempt-1.json"):
                run_id = path.parent.name
                if run_id in children or store.read(run_id, "attempt-1") or store.read(run_id, "attempt-1-intent"):
                    continue
                request = store.read(run_id, "request", required=True)
                spec = request.get("evaluation")
                if spec:
                    if "task_id" in spec and (store.root / "stop").exists():
                        continue
                    ready = store.read(run_id, "harbor-ready")
                    if (not ready or not store.read(run_id, "provider-start")
                            or not store.read(ready["assignment"], "runner-allowed")):
                        continue
                    binding = Binding(store.root, spec["rollout"])
                    if not binding.active():
                        store.record(run_id, "attempt-1", {"attempt": 1, "reason": "lease_lost", "usage": None, "billing": None})
                        continue
                log = store.run_dir(run_id) / "bridge-runner.log"
                with log.open("ab") as output:
                    children[run_id] = subprocess.Popen([
                        sys.executable, "-m", "scripts.probe_harbor_runner", "run", str(store.root),
                        cli, str(sentinel), str(records), run_id,
                    ], stdout=output, stderr=subprocess.STDOUT)
            sleep(0.15)
    finally:
        if resume_at is not None:
            subprocess.run(["docker", "unpause", server], check=False, stdout=subprocess.DEVNULL)
    for child in children.values():
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            continue


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "watch", "run", "check-live-transport"])
    parser.add_argument("root", type=Path)
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()
    store = Evidence(args.root)
    if args.mode == "prepare":
        sha, image, worker, transport = args.args
        prepare(store, sha, image, worker, transport == "live")
    elif args.mode == "check-live-transport":
        expected = store.read("experiment", "configuration", required=True)["transport"]
        if proxy_from_env().pin(store) != expected:
            raise Conflict("CLI Proxy Hub transport differs from the frozen pilot")
    else:
        cli, sentinel, records, target = args.args
        if args.mode == "watch":
            watch(store, cli, Path(sentinel), Path(records), target)
        else:
            run_one(store, target, cli, Path(sentinel), Path(records))


if __name__ == "__main__":
    main()
