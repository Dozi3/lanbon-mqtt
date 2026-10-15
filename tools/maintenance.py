"""Conservative GitHub-token maintenance; never executes pull-request code.

Only Ruff patch pins and documentation can be released unattended. Every merge
uses an exact head SHA, current main ancestry and the real Validate check suite.
Run this file from the trusted default-branch checkout, never a PR checkout.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MANIFEST = "custom_components/lanbon_mqtt/manifest.json"
CHANGELOG = "CHANGELOG.md"
REQUIRED_CHECKS = {"Tests, lint and package", "Hassfest", "HACS"}
MARKER = "<!-- lanbon-maintenance-release:v1 -->"
DEPENDABOT = ("dependabot[bot]", 49699333)
ACTIONS_BOT = ("github-actions[bot]", 41898282)
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


class PolicyError(ValueError):
    """An invariant was not proved; no dependent mutation is allowed."""


def require(condition, message):
    if not condition:
        raise PolicyError(message)


def version(value):
    match = SEMVER.fullmatch(value) if isinstance(value, str) else None
    require(match is not None, "Expected an exact stable X.Y.Z version")
    return tuple(map(int, match.groups()))


def next_patch(value):
    major, minor, patch = version(value)
    return f"{major}.{minor}.{patch + 1}"


def is_bot(user, identity):
    return (
        user.get("login") == identity[0]
        and user.get("id") == identity[1]
        and user.get("type") == "Bot"
    )


def ruff_pin(project):
    require(project.get("project", {}).get("dependencies") == [], "Runtime dependencies changed")
    groups = project.get("dependency-groups", {})
    dev = groups.get("dev", [])
    pins = [item for item in dev if isinstance(item, str) and item.lower().startswith("ruff")]
    require(len(pins) == 1, "Exactly one Ruff development pin is required")
    match = re.fullmatch(r"ruff==(\d+\.\d+\.\d+)", pins[0])
    require(match is not None, "Ruff must use a plain exact stable version pin")
    version(match.group(1))
    return match.group(1)


def check_ruff_update(before_project, after_project, before_lock, after_lock):
    """Check parsed structure, including nested lock metadata and all other packages."""
    old = tomllib.loads(before_project)
    new = tomllib.loads(after_project)
    old_pin, new_pin = ruff_pin(old), ruff_pin(new)
    old_version, new_version = version(old_pin), version(new_pin)
    require(
        old_version[:2] == new_version[:2] and new_version[2] > old_version[2],
        "Only a Ruff patch increase within its current major/minor is unattended",
    )
    expected = copy.deepcopy(old)
    dev = expected["dependency-groups"]["dev"]
    dev[dev.index(f"ruff=={old_pin}")] = f"ruff=={new_pin}"
    require(expected == new, "pyproject.toml contains changes beyond the Ruff pin")

    locks = [tomllib.loads(before_lock), tomllib.loads(after_lock)]
    normalized = []
    for lock, pin in zip(locks, (old_pin, new_pin), strict=True):
        value = copy.deepcopy(lock)
        packages = value.get("package", [])
        matches = [item for item in packages if item.get("name") == "ruff"]
        require(len(matches) == 1, "Lock must contain exactly one Ruff package")
        package = matches[0]
        require(package.get("version") == pin, "Ruff lock version does not match the project")
        require(
            package.get("source") == {"registry": "https://pypi.org/simple"},
            "Ruff must remain sourced from PyPI",
        )
        require(
            set(package) == {"name", "version", "source", "sdist", "wheels"},
            "Unexpected Ruff lock fields or dependencies",
        )
        artifacts = [package["sdist"], *package["wheels"]]
        require(bool(package["wheels"]), "Ruff wheels are required")
        for artifact in artifacts:
            url = urllib.parse.urlsplit(artifact.get("url", ""))
            require(
                url.scheme == "https"
                and url.netloc == "files.pythonhosted.org"
                and not url.query
                and not url.fragment
                and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("hash", ""))
                and f"ruff-{pin}" in url.path,
                "Untrusted Ruff distribution artifact",
            )
        packages.remove(package)
        metadata_pins = []
        for item in packages:
            dependencies = item.get("metadata", {}).get("requires-dev", {})
            for dependencies_group in dependencies.values():
                for dependency in dependencies_group:
                    if dependency.get("name") == "ruff":
                        metadata_pins.append(dependency)
        require(
            metadata_pins == [{"name": "ruff", "specifier": f"=={pin}"}],
            "Unexpected Ruff development lock metadata",
        )
        metadata_pins[0]["specifier"] = "==NORMALIZED"
        normalized.append(value)
    require(normalized[0] == normalized[1], "Another lock package or metadata changed")


def check_pr_identity(pr, repo, bot, *, ready=True):
    require(is_bot(pr.get("user", {}), bot), "Pull request is not from the expected GitHub bot")
    require(pr.get("state") == "open" and not pr.get("draft"), "Pull request is not ready")
    for side in ("base", "head"):
        require(pr.get(side, {}).get("repo", {}).get("full_name") == repo, "Foreign repository")
    require(pr["base"].get("ref") == "main", "Pull request does not target main")
    if ready:
        require(
            pr.get("mergeable") is True and pr.get("mergeable_state") == "clean",
            "GitHub has not confirmed a clean, unblocked merge",
        )


def authentic_validate_run(run, sha, repo):
    return (
        run.get("head_sha") == sha
        and run.get("name") == "Validate"
        and run.get("path") == ".github/workflows/validate.yml"
        and run.get("repository", {}).get("full_name") == repo
        and run.get("head_repository", {}).get("full_name") == repo
        and run.get("event") in {"push", "pull_request", "workflow_dispatch"}
    )


def check_ci(sha, checks, statuses, runs, repo):
    """Tie all required results to one successful, authentic Validate workflow run."""
    require(bool(checks), "No check runs exist for this exact head")
    for check in checks:
        require(check.get("head_sha") == sha, "A check belongs to another commit")
        require(check.get("status") == "completed", "A check is still pending")
        require(check.get("conclusion") in {"success", "neutral", "skipped"}, "A check failed")
    require(
        all(item.get("state") == "success" for item in statuses),
        "A legacy commit status is pending or failed",
    )
    valid_runs = [
        run
        for run in runs
        if authentic_validate_run(run, sha, repo)
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and isinstance(run.get("check_suite_id"), int)
        and run["check_suite_id"] > 0
    ]
    for run in valid_runs:
        names = {
            check.get("name")
            for check in checks
            if check.get("conclusion") == "success"
            and check.get("app", {}).get("id") == 15368
            and check.get("app", {}).get("slug") == "github-actions"
            and check.get("check_suite", {}).get("id") == run.get("check_suite_id")
        }
        if REQUIRED_CHECKS <= names:
            return
    raise PolicyError("Exact-head successful Validate workflow and required checks are missing")


def safe_paths(files):
    """Documentation/dev-only updates cannot touch integration or automation code."""
    require(bool(files), "There are no changes to release")
    for item in files:
        name = item["filename"]
        require(item.get("status") != "renamed", "Renamed files require human review")
        parts = name.split("/")
        require(all(part not in {"", ".", ".."} for part in parts), "Invalid file path")
        require(
            name.startswith("docs/")
            or (len(parts) == 1 and name.endswith(".md"))
            or name in {"pyproject.toml", "uv.lock"},
            f"Human review required for {name}",
        )


def release_section(target):
    version(target)
    return (
        f"\n## {target}\n\n"
        "- Maintenance release containing validated documentation and/or Ruff patch updates.\n"
        "- Device control runtime and configuration schema are unchanged.\n"
    )


def release_changes(manifest, changelog, old_version):
    """Preserve all manifest bytes except its version; use reproducible release prose."""
    target = next_patch(old_version)
    parsed = json.loads(manifest)
    require(parsed.get("version") == old_version, "Main manifest does not match stable baseline")
    pattern = re.compile(r'("version"\s*:\s*")' + re.escape(old_version) + r'(")')
    updated, count = pattern.subn(lambda match: match[1] + target + match[2], manifest)
    require(count == 1, "Manifest version token is not unique")
    require(changelog.startswith("# Changelog\n"), "Unexpected changelog heading")
    require(f"\n## {target}\n" not in changelog, "Target version is already documented")
    notes = changelog.replace("# Changelog\n", "# Changelog\n" + release_section(target), 1)
    return {MANIFEST: updated, CHANGELOG: notes}


class GitHub:
    """Small fixed-origin REST boundary; dry-run forbids every write request."""

    def __init__(self, repo, token, dry_run=False):
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo), "Invalid GH_REPO")
        require(bool(token), "GH_TOKEN is required")
        self.repo = repo
        self.token = token
        self.dry_run = dry_run

    def request(self, method, path, data=None, optional=False):
        require(path == "" or path.startswith("/"), "REST path must be repository-relative")
        if method != "GET" and self.dry_run:
            raise PolicyError(f"Dry-run write blocked: {method} {path}")
        body = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "lanbon-mqtt-maintenance",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            if optional and error.code == 404:
                return None
            raise PolicyError(f"GitHub {method} {path} returned HTTP {error.code}") from None

    def pages(self, path, key=None):
        values = []
        for page in range(1, 6):
            separator = "&" if "?" in path else "?"
            result = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            items = result[key] if key else result
            values.extend(items)
            if len(items) < 100:
                return values
        raise PolicyError("Too many API results for unattended maintenance")

    def content(self, path, ref):
        result = self.request(
            "GET", f"/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref, safe='')}"
        )
        require(result.get("encoding") == "base64", "Expected a regular base64 file")
        return base64.b64decode(result["content"]).decode("utf-8")

    def compare(self, base, head):
        result = self.request("GET", f"/compare/{base}...{head}")
        require(result.get("behind_by") == 0, "Head is not up to date with the baseline")
        require(result.get("status") in {"ahead", "identical"}, "Diverged commit ancestry")
        files = result.get("files", [])
        require(len(files) < 300, "GitHub comparison may have truncated the changed files")
        return files


class Maintenance:
    def __init__(self, github):
        self.api = github
        self.notes = []

    def note(self, message):
        self.notes.append(message)
        print(message)

    def dispatch(self, workflow, ref):
        self.api.request("POST", f"/actions/workflows/{workflow}/dispatches", {"ref": ref})

    def head(self):
        return self.api.request("GET", "/git/ref/heads/main")["object"]["sha"]

    def approved(self, pr, main):
        require(pr["base"]["sha"] == main, "Pull request base snapshot is stale")
        self.api.compare(main, pr["head"]["sha"])
        sha = pr["head"]["sha"]
        runs = self.api.pages(
            f"/actions/workflows/validate.yml/runs?head_sha={sha}", "workflow_runs"
        )
        authentic = [run for run in runs if authentic_validate_run(run, sha, self.api.repo)]
        natural = [run for run in authentic if run.get("event") == "pull_request"]
        newest_natural = max(natural, key=lambda run: run.get("id", 0), default=None)
        if (
            newest_natural
            and newest_natural.get("status") == "completed"
            and newest_natural.get("conclusion") == "action_required"
        ):
            return self.retry_approval_run(pr, newest_natural)
        executable = [run for run in authentic if run.get("conclusion") != "action_required"]
        if not executable:
            if self.api.dry_run:
                self.note(f"Dry run: would dispatch missing Validate run for {sha}")
            else:
                self.dispatch("validate.yml", pr["head"]["ref"])
                self.note(f"Requested missing Validate run for {sha}; awaiting its result")
            return False
        newest = max(executable, key=lambda run: run.get("id", 0))
        if newest.get("status") != "completed":
            self.note(f"Validate is still running for {sha}; awaiting its result")
            return False
        require(newest.get("conclusion") == "success", "Latest executable Validate run failed")
        checks = self.api.pages(f"/commits/{sha}/check-runs?filter=latest", "check_runs")
        statuses = self.api.pages(f"/commits/{sha}/statuses")
        # The statuses endpoint includes superseded statuses; conservative gating
        # deliberately leaves such PRs for human review rather than guessing.
        for check in checks:
            if check.get("status") == "completed":
                require(
                    check.get("conclusion") in {"success", "neutral", "skipped"},
                    "A completed check failed",
                )
        require(
            all(item.get("state") in {"success", "pending"} for item in statuses),
            "A legacy commit status failed",
        )
        if any(check.get("status") != "completed" for check in checks) or any(
            item.get("state") == "pending" for item in statuses
        ):
            self.note(f"Checks are still pending for {sha}; awaiting their results")
            return False
        check_ci(sha, checks, statuses, runs, self.api.repo)
        require(self.head() == main, "Main advanced while checking this pull request")
        current = self.api.request("GET", f"/pulls/{pr['number']}")
        require(current["head"]["sha"] == sha, "Pull request head advanced during validation")
        check_pr_identity(current, self.api.repo, (pr["user"]["login"], pr["user"]["id"]))
        return True

    def retry_approval_run(self, pr, run):
        workflow = self.api.request("GET", "/actions/workflows/validate.yml")
        user = pr.get("user", {})
        require(
            authentic_validate_run(run, pr["head"]["sha"], self.api.repo)
            and run.get("event") == "pull_request"
            and run.get("status") == "completed"
            and run.get("conclusion") == "action_required"
            and (is_bot(user, ACTIONS_BOT) or is_bot(user, DEPENDABOT))
            and is_bot(run.get("actor", {}), (user.get("login"), user.get("id")))
            and isinstance(run.get("id"), int)
            and run["id"] > 0
            and isinstance(workflow.get("id"), int)
            and workflow["id"] > 0
            and run.get("workflow_id") == workflow["id"]
            and any(item.get("number") == pr["number"] for item in run.get("pull_requests", [])),
            "Approval-gated run provenance could not be proved; human action is required",
        )
        require(
            run.get("run_attempt") == 1,
            "Approval-gated Validate rerun already attempted or unknown; human action is required",
        )
        if self.api.dry_run:
            self.note(f"Dry run: would rerun original approval-gated Validate run {run['id']}")
        else:
            self.api.request("POST", f"/actions/runs/{run['id']}/rerun", {})
            self.note(f"Requested original Validate run {run['id']} rerun; awaiting its result")
        return False

    def ruff_change(self, base, head):
        files = self.api.compare(base, head)
        require(
            {item["filename"] for item in files} == {"pyproject.toml", "uv.lock"}
            and all(item.get("status") == "modified" for item in files),
            "Dependency PR must modify exactly pyproject.toml and uv.lock",
        )
        check_ruff_update(
            self.api.content("pyproject.toml", base),
            self.api.content("pyproject.toml", head),
            self.api.content("uv.lock", base),
            self.api.content("uv.lock", head),
        )

    def safe_changes(self, stable, main):
        files = self.api.compare(stable, main)
        safe_paths(files)
        names = {item["filename"] for item in files}
        if names & {"pyproject.toml", "uv.lock"}:
            require(
                {"pyproject.toml", "uv.lock"} <= names,
                "Development updates require both project and lock files",
            )
            check_ruff_update(
                self.api.content("pyproject.toml", stable),
                self.api.content("pyproject.toml", main),
                self.api.content("uv.lock", stable),
                self.api.content("uv.lock", main),
            )
        return files

    def merge(self, pr, *, validate_main=True):
        require(
            self.head() == pr["base"]["sha"],
            "Main advanced before merge; leave this pull request for fresh validation",
        )
        if self.api.dry_run:
            self.note(f"Dry run: would squash merge PR #{pr['number']} at {pr['head']['sha']}")
            return None
        result = self.api.request(
            "PUT",
            f"/pulls/{pr['number']}/merge",
            {"sha": pr["head"]["sha"], "merge_method": "squash"},
        )
        require(result.get("merged") is True, "GitHub did not confirm the merge")
        self.note(f"Merged PR #{pr['number']} at {result['sha']}")
        if validate_main:
            self.dispatch("validate.yml", "main")
        return result["sha"]

    def expected(self, main, stable_version):
        return release_changes(
            self.api.content(MANIFEST, main),
            self.api.content(CHANGELOG, main),
            stable_version,
        )

    def candidate(self, pr, main, stable_version):
        check_pr_identity(pr, self.api.repo, ACTIONS_BOT, ready=False)
        target = next_patch(stable_version)
        require(
            pr["head"].get("ref") == f"automation/release-{target}", "Unexpected release branch"
        )
        require(self.matches_body(pr.get("body"), target), "Release PR marker/body changed")
        try:
            self.check_generated(main, pr["head"]["sha"], stable_version)
        except PolicyError:
            self.prove_stale_generated(main, pr["head"]["sha"], stable_version)
            if self.api.dry_run:
                self.note(f"Dry run: would update stale release PR #{pr['number']} using GitHub")
                return False
            require(self.head() == main, "Main advanced before the release branch update")
            self.api.request(
                "PUT",
                f"/pulls/{pr['number']}/update-branch",
                {"expected_head_sha": pr["head"]["sha"]},
            )
            self.dispatch("validate.yml", pr["head"]["ref"])
            self.note(f"Updated release PR #{pr['number']} from main; awaiting fresh validation")
            return False
        return True

    def prove_stale_generated(self, main, head, stable_version):
        comparison = self.api.request("GET", f"/compare/{main}...{head}")
        base = comparison.get("merge_base_commit", {}).get("sha")
        require(
            isinstance(base, str)
            and re.fullmatch(r"[0-9a-f]{40}", base) is not None
            and base != main,
            "Release branch is not a provably stale generated branch; human review is required",
        )
        self.api.compare(base, main)
        self.check_generated(base, head, stable_version)

    def check_generated(self, base, head, stable_version):
        files = self.api.compare(base, head)
        require(
            {item["filename"] for item in files} == {MANIFEST, CHANGELOG}
            and all(item.get("status") == "modified" for item in files),
            "Release PR contains changes beyond generated version and changelog",
        )
        for path, contents in self.expected(base, stable_version).items():
            require(self.api.content(path, head) == contents, f"Unexpected generated {path}")

    @staticmethod
    def body(target):
        return (
            f"{MARKER}\n\nAutomated maintenance release {target}.\n\n"
            "Only validated documentation and Ruff patch updates are eligible. "
            "Device runtime changes require human review."
        )

    @classmethod
    def matches_body(cls, body, target):
        canonical = cls.body(target)
        return isinstance(body, str) and (
            body == canonical or body.startswith(canonical + "\n\n## Summary by Sourcery\n\n")
        )

    def merged_release_pr(self, pr, target):
        return (
            is_bot(pr.get("user", {}), ACTIONS_BOT)
            and pr.get("state") == "closed"
            and bool(pr.get("merged_at"))
            and isinstance(pr.get("merge_commit_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", pr["merge_commit_sha"]) is not None
            and pr.get("base", {}).get("ref") == "main"
            and pr.get("base", {}).get("repo", {}).get("full_name") == self.api.repo
            and pr.get("head", {}).get("repo", {}).get("full_name") == self.api.repo
            and pr.get("head", {}).get("ref") == f"automation/release-{target}"
            and self.matches_body(pr.get("body"), target)
        )

    def publish(self, sha, stable_sha, stable_version):
        """Recover a merge/tag/dispatch crash only from the exact generated bot PR."""
        target = next_patch(stable_version)
        commit = self.api.request("GET", f"/commits/{sha}")
        require(len(commit.get("parents", [])) == 1, "Expected a squash release commit")
        parent = commit["parents"][0]["sha"]
        self.safe_changes(stable_sha, parent)
        self.check_generated(parent, sha, stable_version)
        prs = self.api.pages(f"/commits/{sha}/pulls")
        require(
            any(
                self.merged_release_pr(pr, target) and pr.get("merge_commit_sha") == sha
                for pr in prs
            ),
            "No matching merged GitHub Actions release PR exists",
        )
        tag = f"v{target}"
        release = self.api.request("GET", f"/releases/tags/{tag}", optional=True)
        # A failed upload can leave a draft. The release workflow verifies and
        # resumes that draft; this helper only redispatches its immutable tag.
        existing = self.api.request("GET", f"/git/ref/tags/{tag}", optional=True)
        if existing:
            require(
                existing.get("object", {}).get("sha") == sha
                and existing.get("object", {}).get("type") == "commit",
                "Existing tag does not point directly to the verified merge commit",
            )
        if release and not release.get("draft"):
            require(not release.get("prerelease"), "Release is not stable")
            require(existing is not None, "Published release is missing its immutable tag")
            assets = release.get("assets", [])
            require(
                {asset.get("name") for asset in assets}
                == {f"lanbon-mqtt-{target}.zip", "SHA256SUMS", "release-receipt.json"}
                and len(assets) == 3
                and all(
                    asset.get("state") == "uploaded"
                    and isinstance(asset.get("size"), int)
                    and asset["size"] > 0
                    for asset in assets
                ),
                "Published release assets are missing, incomplete or unexpected",
            )
            self.note(f"Stable release {tag} already exists; nothing to publish")
            return
        runs = self.api.pages(
            f"/actions/workflows/release.yml/runs?head_sha={sha}", "workflow_runs"
        )
        if any(
            run.get("head_sha") == sha
            and run.get("head_branch") == tag
            and run.get("event") == "workflow_dispatch"
            and run.get("status") != "completed"
            for run in runs
        ):
            self.note(f"Release {tag} is awaiting an existing release.yml run")
            return
        if self.api.dry_run:
            self.note(f"Dry run: would create immutable {tag} at {sha} and dispatch release.yml")
            return
        if not existing:
            self.api.request("POST", "/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
        self.dispatch("release.yml", tag)
        self.note(f"Dispatched release.yml for immutable {tag} at {sha}")

    def create_candidate(self, main, stable_version):
        target = next_patch(stable_version)
        branch = f"automation/release-{target}"
        expected = self.expected(main, stable_version)
        existing = self.api.request("GET", f"/git/ref/heads/{branch}", optional=True)
        if self.api.dry_run:
            self.note(f"Dry run: would create or recover release PR for {target}")
            return
        require(
            os.environ.get("LANBON_RELEASE_PRS_ENABLED") == "true",
            "Release pull-request creation is disabled; "
            "LANBON_RELEASE_PRS_ENABLED must be explicitly set to true after owner approval",
        )
        if existing:
            sha = existing["object"]["sha"]
            commit = self.api.request("GET", f"/commits/{sha}")
            if [parent["sha"] for parent in commit.get("parents", [])] == [main]:
                self.check_generated(main, sha, stable_version)
            else:
                self.prove_stale_generated(main, sha, stable_version)
                self.note(
                    "Recovered a proven generated orphan branch; its PR will update from main"
                )
        else:
            commit = self.api.request("GET", f"/git/commits/{main}")
            tree = self.api.request(
                "POST",
                "/git/trees",
                {
                    "base_tree": commit["tree"]["sha"],
                    "tree": [
                        {"path": path, "mode": "100644", "type": "blob", "content": content}
                        for path, content in expected.items()
                    ],
                },
            )
            created = self.api.request(
                "POST",
                "/git/commits",
                {
                    "message": f"Release {target}",
                    "tree": tree["sha"],
                    "parents": [main],
                    "author": {
                        "name": "github-actions[bot]",
                        "email": "41898282+github-actions[bot]@users.noreply.github.com",
                    },
                },
            )
            self.api.request(
                "POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": created["sha"]}
            )
        require(self.head() == main, "Main advanced before release PR creation")
        pr = self.api.request(
            "POST",
            "/pulls",
            {
                "title": f"Release {target}",
                "head": branch,
                "base": "main",
                "body": self.body(target),
            },
        )
        self.note(f"Created release PR #{pr['number']} for {target}")
        # GITHUB_TOKEN-created PR runs may require approval; explicitly dispatch validation.
        self.dispatch("validate.yml", branch)

    def run(self):
        repo = self.api.request("GET", "")
        require(repo.get("default_branch") == "main", "Default branch must remain main")
        stable = self.api.request("GET", "/releases/latest", optional=True)
        require(
            stable and not stable.get("draft") and not stable.get("prerelease"),
            "No stable baseline",
        )
        stable_version = stable["tag_name"].removeprefix("v")
        version(stable_version)
        require(stable["tag_name"] == f"v{stable_version}", "Stable tag must use vX.Y.Z")
        stable_sha = self.api.request("GET", f"/commits/{stable['tag_name']}")["sha"]
        require(
            json.loads(self.api.content(MANIFEST, stable_sha)).get("version") == stable_version,
            "Stable release tag and manifest disagree",
        )
        main = self.head()
        current = json.loads(self.api.content(MANIFEST, main)).get("version")
        if current != stable_version:
            require(
                current == next_patch(stable_version), "Main has an unexpected version transition"
            )
            merged = [
                pr
                for pr in self.api.pages("/pulls?state=closed&base=main")
                if self.merged_release_pr(pr, current)
            ]
            require(
                len(merged) == 1, "Expected exactly one matching merged release PR for recovery"
            )
            release_sha = merged[0]["merge_commit_sha"]
            self.api.compare(release_sha, main)
            self.publish(release_sha, stable_sha, stable_version)
            return
        prs = self.api.pages("/pulls?state=open&base=main")
        for item in prs:
            if (
                item.get("head", {}).get("ref")
                == f"automation/release-{next_patch(stable_version)}"
            ):
                pr = self.api.request("GET", f"/pulls/{item['number']}")
                self.safe_changes(stable_sha, main)
                if not self.candidate(pr, main, stable_version):
                    return
                if not self.approved(pr, main):
                    return
                sha = self.merge(pr, validate_main=False)
                if sha:
                    self.publish(sha, stable_sha, stable_version)
                return
        for item in prs:
            if not is_bot(item.get("user", {}), DEPENDABOT):
                continue
            try:
                pr = self.api.request("GET", f"/pulls/{item['number']}")
                check_pr_identity(pr, self.api.repo, DEPENDABOT, ready=False)
                self.ruff_change(main, pr["head"]["sha"])
                if not self.approved(pr, main):
                    return
            except PolicyError as error:
                self.note(f"PR #{item['number']} left for review: {error}")
                continue
            self.merge(pr)
            return
        if not self.api.compare(stable_sha, main):
            self.note("Main matches the stable release; no new release is needed")
            return
        self.safe_changes(stable_sha, main)
        self.create_candidate(main, stable_version)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Read-only planning; no GitHub writes"
    )
    options = parser.parse_args(argv)
    maintenance = None
    result = 0
    try:
        api = GitHub(os.environ.get("GH_REPO", ""), os.environ.get("GH_TOKEN", ""), options.dry_run)
        maintenance = Maintenance(api)
        maintenance.run()
    except (
        PolicyError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        message = f"Maintenance stopped safely: {error}"
        if maintenance:
            maintenance.note(message)
        else:
            print(message)
        result = 1
    except (OSError, urllib.error.URLError) as error:
        # Do not echo headers, tokens or remote bodies in failures.
        message = f"Maintenance stopped safely: network/filesystem error ({type(error).__name__})"
        if maintenance:
            maintenance.note(message)
        else:
            print(message)
        result = 1
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        notes = maintenance.notes if maintenance else ["Maintenance could not initialize safely."]
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write("## Conservative maintenance\n\n" + "\n\n".join(notes) + "\n")
    return result


if __name__ == "__main__":
    sys.exit(main())
