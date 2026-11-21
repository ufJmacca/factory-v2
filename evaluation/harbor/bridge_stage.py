import json
import os
from pathlib import Path

from harbor.models.task.task import Task

from evaluation.harbor.calibrate import docker
from evaluation.harbor.prepare import stage
from factory.evidence import Evidence, encode


def main():
    root = Path(os.environ["BRIDGE_ROOT"])
    pins, path, _ = stage(root, Path("/p03"), Path("/records"), Path("/held-out"))
    history = pins["historical"]
    image = history["workspace_image"]
    docker("tag", image, "factory-harbor-workspace:" + image.removeprefix("sha256:"))
    store = Evidence(root)
    grader = store.blob(encode({
        "files": pins["grader"], "fixture_tree": history["records_tree"], "historical_grader": history["grader"],
        "expected_patch": history["expected_patch"], "workspace": image, "base": history["base_sha"],
    }))
    source = Path("/p03/runs") / history["run_id"] / "request.json"
    historical_request = json.loads(source.read_bytes())
    store.record("bridge", "frozen", {
        "task": str(path), "task_checksum": Task(path).checksum, "task_digest": store.blob(encode(pins)),
        "grader_digest": grader, "base_sha": history["base_sha"], "workspace_image": image,
        "instruction": (path / "instruction.md").read_text(),
        "harbor_revision": pins["harbor_revision"],
        "historical_transport": historical_request["transport"], "fixture_run": historical_request["fixture_run"],
    })


if __name__ == "__main__":
    main()
