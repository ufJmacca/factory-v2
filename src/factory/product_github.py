from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from factory.evidence import Conflict, encode
from factory.product import branch, ci_payload, observation_start, product_marker, pull_request_payload
from factory.product_source import load_candidate
from factory.verify import GIT_ENV, SUBMISSION_REF


def parse_time(value):
    return datetime.fromisoformat(value).timestamp()


def query(endpoint, **fields):
    return endpoint + "?" + urlencode(fields)


def flatten(pages):
    return [value for page in pages for value in page]


def latest(values, key):
    index = {}
    for value in sorted(values, key=lambda item: item["id"]):
        index[key(value)] = value
    return list(index.values())


class GitHub:
    def __init__(self, store):
        self.store = store
        self.prepared = None

    @staticmethod
    def invoke(args, *, input=None, env=None):
        try:
            return subprocess.run(args, input=input, env=env, check=True, timeout=25,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise OSError(f"{args[0]} did not confirm the GitHub operation") from error

    def api(self, request, endpoint, *, payload=None, pages=False):
        args = ["gh", "api", "--hostname", "github.com", f"repos/{request['repo']}/{endpoint}"]
        if pages:
            args.extend(("--paginate", "--slurp"))
        if payload is not None:
            args.extend(("--method", "POST", "--input", "-"))
        return json.loads(self.invoke(args, input=encode(payload) if payload is not None else None))

    def git(self, *args, directory=None):
        env = {**GIT_ENV, "PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "GH_PROMPT_DISABLED": "1"}
        for name in ("GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN"):
            if name in os.environ:
                env[name] = os.environ[name]
        command = ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                   "-c", "core.hooksPath=/dev/null"]
        if directory is not None:
            command.extend(("-C", str(directory)))
        return self.invoke([*command, *args], env=env).decode().strip()

    @staticmethod
    def url(request):
        return f"https://github.com/{request['repo']}.git"

    def find_push(self, request):
        ref = "refs/heads/" + branch(request)
        result = self.git("ls-remote", "--refs", self.url(request), ref)
        if result:
            revision, remote_ref = result.split("\t")
            return {"ref": remote_ref, "revision": revision}

    @contextmanager
    def prepare(self, request):
        checkpoint, bundle = load_candidate(self.store, request)
        if request.get("ci_publish"):
            login = json.loads(self.invoke(["gh", "api", "--hostname", "github.com", "user"]))["login"]
            if login != request["ci_publish"]["actor"]:
                raise Conflict("host GitHub account differs from the approved CI publisher")
        if prior := request.get("predecessor"):
            expected = prior["base_sha"] + "\trefs/heads/" + request["base"]
            if self.git("ls-remote", "--refs", self.url(request), f"refs/heads/{request['base']}") != expected:
                raise Conflict("product base changed after independent verification")
        with TemporaryDirectory(prefix="factory-product-") as directory:
            path = Path(directory)
            bundle_file = path / "submission.bundle"
            bundle_file.write_bytes(bundle)
            repo = path / "bare"
            self.git("init", "--bare", "--template=", "--object-format=sha1", str(repo))
            self.git("-c", "fetch.fsckObjects=true", "fetch", "--no-tags", "--no-recurse-submodules",
                     str(bundle_file), SUBMISSION_REF, directory=repo)
            head = self.git("rev-parse", "--verify", "FETCH_HEAD^{commit}", directory=repo)
            if head != request["revision"]:
                raise Conflict("retained bundle does not contain the admitted Git head")
            self.git("merge-base", "--is-ancestor", checkpoint["base_sha"], head, directory=repo)
            if prior := request.get("predecessor"):
                self.git("merge-base", "--is-ancestor", prior["revision"], head, directory=repo)
            self.prepared = (head, repo)
            try:
                yield self
            finally:
                self.prepared = None

    def push(self, request):
        if self.prepared is None or self.prepared[0] != request["revision"]:
            raise Conflict("Git push requires the prepared immutable product revision")
        head, repo = self.prepared
        ref = "refs/heads/" + branch(request)
        previous = request.get("predecessor", {}).get("revision", "")
        self.git("push", "--porcelain", f"--force-with-lease={ref}:{previous}", self.url(request), f"{head}:{ref}",
                 directory=repo)

    def statuses(self, request):
        endpoint = f"commits/{request['revision']}/statuses?per_page=100"
        return latest(flatten(self.api(request, endpoint, pages=True)), lambda value: value["context"])

    def find_status(self, request, _ci):
        for status in self.statuses(request):
            if status["context"] == request["ci_publish"]["name"]:
                return {
                    "context": status["context"], "actor": (status["creator"] or {}).get("login"),
                    "id": status["id"], "revision": request["revision"], "state": status["state"],
                    "description": status["description"],
                }

    def publish_status(self, request, ci):
        self.api(request, f"statuses/{request['revision']}", payload=ci_payload(request, ci))

    def pull(self, request, pr):
        head, base = pr["head"], pr["base"]
        marker = "<!-- factory-run:" + product_marker(request) + " -->"
        body = pr["body"] or ""
        markers = [line.strip() for line in body.splitlines() if line.strip().startswith("<!-- factory-run:")]
        if (
            any((ref.get("repo") or {}).get("full_name", "").lower() != request["repo"].lower()
                for ref in (head, base))
            or head["ref"] != branch(request) or base["ref"] != request["base"]
            or markers != [marker]
        ):
            raise Conflict("GitHub PR head repository, base, product branch, or run marker differs")
        return {
            "number": pr["number"], "url": pr["html_url"], "created_epoch": parse_time(pr["created_at"]),
            "state": pr["state"],
            "head_ref": head["ref"], "base": base["ref"], "revision": head["sha"],
            "marker": product_marker(request), "repo": request["repo"],
        }

    def find_pr(self, request):
        owner = request["repo"].split("/")[0]
        endpoint = query("pulls", state="all", head=f"{owner}:{branch(request)}", per_page=100)
        results = flatten(self.api(request, endpoint, pages=True))
        if len(results) > 1:
            raise Conflict("multiple GitHub pull requests match the product branch")
        if results:
            return self.pull(request, results[0])

    def create_pr(self, request):
        self.api(request, "pulls", payload=pull_request_payload(request))

    def inspect(self, request, pr):
        endpoint = f"pulls/{pr['number']}"
        current = self.api(request, endpoint)
        revision = self.pull(request, current)["revision"]
        result = {"revision": revision, "state": current["state"], "checks": [], "review": None,
                  "corrections": [], "feedback": []}
        if revision != request["revision"] or current["state"] != "open":
            return result
        commit = f"commits/{revision}"
        check_pages = self.api(request, f"{commit}/check-runs?filter=latest&per_page=100", pages=True)
        runs = [run for page in check_pages for run in page["check_runs"]]
        for run in latest(runs, lambda value: (value["name"], (value["app"] or {}).get("slug"))):
            result["checks"].append({
                "kind": "check_run", "name": run["name"], "actor": (run["app"] or {}).get("slug"),
                "id": run["id"], "revision": run["head_sha"],
                "state": run["conclusion"] if run["status"] == "completed" else "pending",
            })
        for status in self.statuses(request):
            result["checks"].append({
                "kind": "status", "name": status["context"], "actor": (status["creator"] or {}).get("login"),
                "id": status["id"], "revision": revision, "state": status["state"],
            })
        reviews = flatten(self.api(request, f"{endpoint}/reviews?per_page=100", pages=True))
        actor = request["reviewer"]
        eligible = [] if current["user"]["login"].lower() == actor.lower() else [
            review for review in reviews if (review["user"] or {}).get("login", "").lower() == actor.lower()
        ]
        for review in sorted(eligible, key=lambda item: item["id"]):
            state = review["state"].lower()
            receipt = {"actor": actor, "revision": review["commit_id"], "state": state, "id": review["id"],
                       "submitted_at": review["submitted_at"]}
            if state in {"approved", "changes_requested", "dismissed"}:
                result["review"] = receipt
            if review["body"]:
                result["corrections"].append({**receipt, "kind": "review", "body": review["body"]})
        for comment in flatten(self.api(request, f"{endpoint}/comments?per_page=100", pages=True)):
            if (comment["user"] or {}).get("login", "").lower() == actor.lower():
                result["corrections"].append({
                    "kind": "line_comment", "id": comment["id"], "actor": actor, "body": comment["body"],
                    "revision": comment["commit_id"], "submitted_at": comment["created_at"],
                })
        issue = f"issues/{pr['number']}/comments?per_page=100"
        for comment in flatten(self.api(request, issue, pages=True)):
            if (comment["user"] or {}).get("login", "").lower() == request["approver"].lower():
                observed_at = parse_time(comment["created_at"])
                start = observation_start(pr)
                if start <= observed_at <= start + request["observation_seconds"]:
                    result["feedback"].append({
                        "kind": "pull_request_comment", "id": comment["id"], "actor": request["approver"],
                        "observed_at": observed_at, "feedback": comment["body"],
                    })
        return result
