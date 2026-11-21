from pathlib import Path
import re
import subprocess
import tomllib

from factory.checkpoint import git
from factory.evidence import Evidence, digest, encode


BOT = "delivery-engineer"
CLI_BUILD = "codex-cli 0.162.1"
TOOLS = ("workspace.read", "workspace.edit", "workspace.test", "submission.create")
FIELDS = {
    "on_event", "workflow", "runtime_profile", "instructions", "skills", "tools",
    "timeout_minutes", "max_attempts", "model", "reasoning_effort",
}


class InvalidProfile(ValueError):
    pass


def require(condition, message: str):
    if not condition:
        raise InvalidProfile(message)


def resolve(repo: Path, revision: str, store: Evidence) -> dict:
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "factory revision must be a full Git SHA")
    try:
        require(git(repo, "rev-parse", f"{revision}^{{commit}}").decode().strip() == revision,
                "factory revision must name a commit")
        source = git(repo, "show", f"{revision}:agents.toml")
        config = tomllib.loads(source.decode())
        require(config.keys() == {"factory"}, "unknown configuration table")
        factory = config["factory"]
        require(factory.keys() == {"schema_version", "bots"} and type(factory["schema_version"]) is int
                and factory["schema_version"] == 1, "unsupported factory schema")
        require(factory["bots"].keys() == {BOT}, "the pilot requires one delivery-engineer")
        profile = factory["bots"][BOT]
        require(profile.keys() == FIELDS, "unknown or missing bot field")
        require(profile["on_event"] == "outcome.approved" and profile["workflow"] == "deliver-outcome",
                "unsupported event or workflow")
        require(profile["runtime_profile"] == "codex-cli-chatgpt", "unsupported runtime")
        model = profile["model"]
        require(isinstance(model, str) and re.fullmatch(r"gpt-[a-z0-9.-]+", model),
                "invalid Codex model")
        require(profile["reasoning_effort"] in ("low", "medium", "high"), "invalid reasoning effort")
        require(isinstance(profile["instructions"], str) and 0 < len(profile["instructions"]) <= 4096,
                "invalid instructions")
        tools = profile["tools"]
        require(isinstance(tools, list) and len(tools) == len(TOOLS) and sorted(tools) == sorted(TOOLS),
                "unsupported tool permissions")
        for field, maximum in (("timeout_minutes", 30), ("max_attempts", 2)):
            require(type(profile[field]) is int and 1 <= profile[field] <= maximum, f"invalid {field}")
        skill_dirs = profile["skills"]
        require(isinstance(skill_dirs, list) and 0 < len(skill_dirs) <= 8
                and all(isinstance(path, str) and re.fullmatch(r"\.agents/skills/[a-z0-9][a-z0-9-]*", path)
                        for path in skill_dirs) and len(set(skill_dirs)) == len(skill_dirs),
                "invalid skill paths")
        files = {}
        size = 0
        for directory in skill_dirs:
            for entry in git(repo, "ls-tree", "-rz", revision, "--", directory + "/").split(b"\0")[:-1]:
                metadata, name = entry.decode().split("\t", 1)
                mode, kind, blob = metadata.split()
                require(kind == "blob" and mode in ("100644", "100755")
                        and re.fullmatch(r"[a-zA-Z0-9._/-]+", name), f"invalid skill file: {name}")
                data = git(repo, "cat-file", "blob", blob)
                size += len(data)
                require(size <= 256 * 1024, "skill bundle exceeds pilot limit")
                files[name] = store.blob(data)
            require(f"{directory}/SKILL.md" in files, f"required skill missing: {directory}")
    except (AttributeError, KeyError, TypeError, UnicodeError, subprocess.SubprocessError,
            tomllib.TOMLDecodeError) as error:
        raise InvalidProfile("invalid or unavailable factory configuration") from error
    return {
        "schema_version": 1, "factory_sha": revision, "bot": BOT,
        "profile": profile, "profile_digest": digest(encode(profile)), "source_digest": store.blob(source),
        "skills": files, "tools": list(TOOLS), "cli_build": CLI_BUILD, "model": profile["model"],
        "reasoning_effort": profile["reasoning_effort"], "auth_mode": "chatgpt", "api_fallback": False,
    }
