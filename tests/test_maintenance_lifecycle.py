"""Exercise maintenance mutations against a stateful repository, without a network."""

import copy
import json

import pytest

from tools.maintenance import Maintenance, PolicyError

REPO = "example/lanbon-mqtt"
MANIFEST = "custom_components/lanbon_mqtt/manifest.json"
CHANGELOG = "CHANGELOG.md"
STABLE = "1" * 40
MAIN = "2" * 40
BRANCH = "automation/release-0.3.1"
TAG = "v0.3.1"
BODY = (
    "<!-- lanbon-maintenance-release:v1 -->\n\nAutomated maintenance release 0.3.1.\n\n"
    "Only validated documentation and Ruff patch updates are eligible. "
    "Device runtime changes require human review."
)
OLD_MANIFEST = '{\n  "domain": "lanbon_mqtt",\n  "version": "0.3.0",\n  "unchanged": 7\n}\n'
OLD_CHANGELOG = "# Changelog\n\n## 0.3.0\n\nOriginal release.\n"
NEW_MANIFEST = OLD_MANIFEST.replace('"0.3.0"', '"0.3.1"')
NEW_CHANGELOG = (
    "# Changelog\n\n## 0.3.1\n\n"
    "- Maintenance release containing validated documentation and/or Ruff patch updates.\n"
    "- Device control runtime and configuration schema are unchanged.\n"
    "\n## 0.3.0\n\nOriginal release.\n"
)


class RepositoryAPI:
    """Validate REST payloads and retain the effects that subsequent runs observe."""

    repo = REPO
    dry_run = False

    def __init__(self):
        stable = {
            MANIFEST: OLD_MANIFEST,
            CHANGELOG: OLD_CHANGELOG,
            "docs/usage.md": "Previous instructions.\n",
            "custom_components/lanbon_mqtt/light.py": "unchanged_device_runtime = True\n",
        }
        main = {**stable, "docs/usage.md": "Improved instructions.\n"}
        self.files = {STABLE: stable, MAIN: main}
        self.parents = {STABLE: [], MAIN: [STABLE]}
        self.trees = {"main-tree": main}
        self.main = MAIN
        self.branches = {}
        self.tags = {}
        self.prs = {}
        self.validation_runs = {}
        self.checks = {}
        self.release_runs = []
        self.published = None
        self.calls = []
        self.fail_pr_once = False
        self.fail_release_dispatch_once = False

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def resolve(self, ref):
        if ref == "v0.3.0":
            return STABLE
        if ref == "main":
            return self.main
        return self.branches.get(ref, ref)

    def content(self, path, ref):
        return self.files[self.resolve(ref)][path]

    def compare(self, base, head):
        base, head = self.resolve(base), self.resolve(head)
        ancestor = head
        while ancestor != base:
            assert self.parents[ancestor], "The comparison must retain baseline ancestry"
            ancestor = self.parents[ancestor][0]
        before, after = self.files[base], self.files[head]
        return [
            {"filename": name, "status": "modified"}
            for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)
        ]

    def pages(self, path, key=None):
        self.calls.append(("GET", path, None))
        if path == "/pulls?state=open&base=main":
            return copy.deepcopy([pr for pr in self.prs.values() if pr["state"] == "open"])
        if path == "/pulls?state=closed&base=main":
            return copy.deepcopy([pr for pr in self.prs.values() if pr["state"] == "closed"])
        if path.startswith("/actions/workflows/validate.yml/runs?head_sha="):
            assert key == "workflow_runs"
            return copy.deepcopy(self.validation_runs.get(path.rsplit("=", 1)[1], []))
        if path.startswith("/commits/") and path.endswith("/check-runs?filter=latest"):
            assert key == "check_runs"
            return copy.deepcopy(self.checks.get(path.split("/")[2], []))
        if path.startswith("/commits/") and path.endswith("/statuses"):
            return []
        if path.startswith("/commits/") and path.endswith("/pulls"):
            sha = path.split("/")[2]
            return copy.deepcopy(
                [pr for pr in self.prs.values() if pr.get("merge_commit_sha") == sha]
            )
        if path.startswith("/actions/workflows/release.yml/runs?head_sha="):
            assert key == "workflow_runs"
            sha = path.rsplit("=", 1)[1]
            return copy.deepcopy([run for run in self.release_runs if run["head_sha"] == sha])
        raise AssertionError((path, key))

    def request(self, method, path, data=None, optional=False):
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == "GET":
            if path == "":
                return {"default_branch": "main"}
            if path == "/releases/latest":
                return {"tag_name": "v0.3.0", "draft": False, "prerelease": False}
            if path == "/commits/v0.3.0":
                return {"sha": STABLE}
            if path == "/git/ref/heads/main":
                return {"object": {"sha": self.main}}
            if path == f"/git/ref/heads/{BRANCH}":
                assert optional
                sha = self.branches.get(BRANCH)
                return {"object": {"sha": sha}} if sha else None
            if path.startswith("/commits/"):
                sha = path.split("/")[2]
                return {"sha": sha, "parents": [{"sha": value} for value in self.parents[sha]]}
            if path == f"/git/commits/{MAIN}":
                return {"tree": {"sha": "main-tree"}}
            if path.startswith("/pulls/"):
                return copy.deepcopy(self.prs[int(path.rsplit("/", 1)[1])])
            if path == f"/releases/tags/{TAG}":
                assert optional
                return self.published
            if path == f"/git/ref/tags/{TAG}":
                assert optional
                value = self.tags.get(TAG)
                return {"object": copy.deepcopy(value)} if value else None
        if method == "POST" and path == "/git/trees":
            assert data["base_tree"] == "main-tree"
            assert data["tree"] == [
                {"path": MANIFEST, "mode": "100644", "type": "blob", "content": NEW_MANIFEST},
                {"path": CHANGELOG, "mode": "100644", "type": "blob", "content": NEW_CHANGELOG},
            ]
            self.trees["release-tree"] = {
                **self.trees["main-tree"],
                **{entry["path"]: entry["content"] for entry in data["tree"]},
            }
            return {"sha": "release-tree"}
        if method == "POST" and path == "/git/commits":
            assert data == {
                "message": "Release 0.3.1",
                "tree": "release-tree",
                "parents": [MAIN],
                "author": {
                    "name": "github-actions[bot]",
                    "email": "41898282+github-actions[bot]@users.noreply.github.com",
                },
            }
            sha = "3" * 40
            assert sha not in self.files, "A release commit must not be recreated during recovery"
            self.files[sha] = copy.deepcopy(self.trees[data["tree"]])
            self.parents[sha] = list(data["parents"])
            return {"sha": sha}
        if method == "POST" and path == "/git/refs":
            ref, sha = data["ref"], data["sha"]
            assert set(data) == {"ref", "sha"}
            assert sha in self.files
            if ref == f"refs/heads/{BRANCH}":
                assert BRANCH not in self.branches
                self.branches[BRANCH] = sha
            else:
                assert ref == f"refs/tags/{TAG}"
                assert TAG not in self.tags, "Existing tags must never be moved or recreated"
                self.tags[TAG] = {"type": "commit", "sha": sha}
            return {"ref": ref, "object": {"sha": sha}}
        if method == "POST" and path == "/pulls":
            assert data == {"title": "Release 0.3.1", "head": BRANCH, "base": "main", "body": BODY}
            if self.fail_pr_once:
                self.fail_pr_once = False
                raise PolicyError("Simulated interruption after branch creation")
            assert not self.prs, "Recovery must not open a duplicate PR"
            self.prs[10] = {
                "number": 10,
                "state": "open",
                "draft": False,
                "mergeable": True,
                "mergeable_state": "clean",
                "body": BODY,
                "user": {"login": "github-actions[bot]", "id": 41898282, "type": "Bot"},
                "base": {"ref": "main", "sha": self.main, "repo": {"full_name": REPO}},
                "head": {
                    "ref": BRANCH,
                    "sha": self.branches[BRANCH],
                    "repo": {"full_name": REPO},
                },
            }
            return copy.deepcopy(self.prs[10])
        if method == "POST" and path == "/actions/workflows/validate.yml/dispatches":
            assert data == {"ref": BRANCH}, "Release PR must not dispatch redundant main validation"
            return None
        if method == "PUT" and path == "/pulls/10/merge":
            pr = self.prs[10]
            assert data == {"sha": pr["head"]["sha"], "merge_method": "squash"}
            assert pr["state"] == "open" and self.main == pr["base"]["sha"]
            merged = "4" * 40
            self.files[merged] = copy.deepcopy(self.files[pr["head"]["sha"]])
            self.parents[merged] = [self.main]
            self.main = merged
            pr.update(state="closed", merged_at="2026-10-15T12:00:00Z", merge_commit_sha=merged)
            return {"merged": True, "sha": merged}
        if method == "POST" and path == "/actions/workflows/release.yml/dispatches":
            assert data == {"ref": TAG}
            target = self.tags[TAG]
            assert target["type"] == "commit"
            self.compare(target["sha"], self.main)
            if self.fail_release_dispatch_once:
                self.fail_release_dispatch_once = False
                raise PolicyError("Simulated interruption after immutable tag creation")
            self.release_runs.append(
                {
                    "head_sha": target["sha"],
                    "head_branch": TAG,
                    "event": "workflow_dispatch",
                    "status": "queued",
                }
            )
            return None
        raise AssertionError((method, path, data))

    def complete_validation(self):
        sha = self.prs[10]["head"]["sha"]
        self.validation_runs[sha] = [
            {
                "head_sha": sha,
                "name": "Validate",
                "path": ".github/workflows/validate.yml",
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": REPO},
                "head_repository": {"full_name": REPO},
                "event": "workflow_dispatch",
                "check_suite_id": 100,
            }
        ]
        self.checks[sha] = [
            {
                "name": name,
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 15368, "slug": "github-actions"},
                "check_suite": {"id": 100},
            }
            for name in ("Tests, lint and package", "Hassfest", "HACS")
        ]


@pytest.fixture
def repository(monkeypatch):
    monkeypatch.setenv("LANBON_RELEASE_PRS_ENABLED", "true")
    return RepositoryAPI()


def test_create_candidate_writes_only_expected_version_and_changelog(repository):
    Maintenance(repository).run()
    assert [path for _, path, _ in repository.writes] == [
        "/git/trees",
        "/git/commits",
        "/git/refs",
        "/pulls",
        "/actions/workflows/validate.yml/dispatches",
    ]
    candidate = repository.branches[BRANCH]
    assert repository.main == MAIN
    assert repository.parents[candidate] == [MAIN]
    assert {item["filename"] for item in repository.compare(MAIN, candidate)} == {
        MANIFEST,
        CHANGELOG,
    }
    assert repository.files[candidate][MANIFEST] == NEW_MANIFEST
    assert repository.files[candidate][CHANGELOG] == NEW_CHANGELOG
    assert repository.files[STABLE][MANIFEST] == OLD_MANIFEST
    assert not repository.tags


def test_branch_created_before_interruption_is_reused_without_rewriting(repository):
    repository.fail_pr_once = True
    with pytest.raises(PolicyError, match="after branch creation"):
        Maintenance(repository).run()
    original_branch = repository.branches[BRANCH]
    assert not repository.prs
    before = len(repository.writes)
    Maintenance(repository).run()
    assert repository.branches[BRANCH] == original_branch
    assert [path for _, path, _ in repository.writes[before:]] == [
        "/pulls",
        "/actions/workflows/validate.yml/dispatches",
    ]
    assert len(repository.prs) == 1


@pytest.mark.parametrize("stale_run", [False, True])
def test_blocked_candidate_without_exact_head_validation_dispatches_only_once(
    repository, stale_run
):
    Maintenance(repository).run()
    pr = repository.prs[10]
    pr.update(mergeable=False, mergeable_state="blocked")
    candidate = pr["head"]["sha"]
    if stale_run:
        repository.validation_runs[candidate] = [{"head_sha": STABLE}]
    before = len(repository.writes)
    Maintenance(repository).run()
    assert repository.writes[before:] == [
        ("POST", "/actions/workflows/validate.yml/dispatches", {"ref": BRANCH})
    ]
    assert repository.main == MAIN
    assert not repository.tags


def test_stale_successful_check_cannot_merge_current_release_candidate(repository):
    Maintenance(repository).run()
    repository.complete_validation()
    candidate = repository.prs[10]["head"]["sha"]
    repository.checks[candidate][0]["head_sha"] = STABLE
    before = len(repository.writes)
    with pytest.raises(PolicyError, match="another commit"):
        Maintenance(repository).run()
    assert repository.main == MAIN
    assert repository.prs[10]["state"] == "open"
    assert not repository.tags
    assert len(repository.writes) == before


def test_validated_candidate_merges_exact_head_tags_merge_and_dispatches_once(repository):
    Maintenance(repository).run()
    repository.complete_validation()
    candidate = repository.branches[BRANCH]
    before = len(repository.writes)
    Maintenance(repository).run()
    assert repository.writes[before:] == [
        ("PUT", "/pulls/10/merge", {"sha": candidate, "merge_method": "squash"}),
        ("POST", "/git/refs", {"ref": f"refs/tags/{TAG}", "sha": repository.main}),
        ("POST", "/actions/workflows/release.yml/dispatches", {"ref": TAG}),
    ]
    assert repository.main != candidate
    assert repository.parents[repository.main] == [MAIN]
    assert repository.tags[TAG] == {"type": "commit", "sha": repository.main}
    assert repository.files[repository.main] == repository.files[candidate]
    before = len(repository.writes)
    Maintenance(repository).run()
    assert len(repository.writes) == before, "Queued publication must not be dispatched twice"


def test_retry_after_tag_creation_dispatches_without_recreating_immutable_tag(repository):
    Maintenance(repository).run()
    repository.complete_validation()
    repository.fail_release_dispatch_once = True
    with pytest.raises(PolicyError, match="after immutable tag creation"):
        Maintenance(repository).run()
    original_tag = copy.deepcopy(repository.tags[TAG])
    assert not repository.release_runs
    before = len(repository.writes)
    Maintenance(repository).run()
    assert repository.tags[TAG] == original_tag
    assert repository.writes[before:] == [
        ("POST", "/actions/workflows/release.yml/dispatches", {"ref": TAG})
    ]


@pytest.mark.parametrize("tag", [{"type": "commit", "sha": STABLE}, {"type": "tag", "sha": MAIN}])
def test_retry_refuses_to_repoint_or_publish_an_inconsistent_existing_tag(repository, tag):
    Maintenance(repository).run()
    repository.complete_validation()
    repository.fail_release_dispatch_once = True
    with pytest.raises(PolicyError):
        Maintenance(repository).run()
    repository.tags[TAG] = copy.deepcopy(tag)
    before = len(repository.writes)
    with pytest.raises(PolicyError, match="Existing tag does not point directly"):
        Maintenance(repository).run()
    assert repository.tags[TAG] == tag
    assert len(repository.writes) == before


def test_already_published_stable_release_has_no_follow_up_mutations(repository):
    Maintenance(repository).run()
    repository.complete_validation()
    Maintenance(repository).run()
    repository.published = {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": name, "state": "uploaded", "size": size}
            for name, size in (
                ("lanbon-mqtt-0.3.1.zip", 300000),
                ("SHA256SUMS", 93),
                ("release-receipt.json", 200),
            )
        ],
    }
    before = len(repository.writes)
    Maintenance(repository).run()
    assert len(repository.writes) == before
    assert json.loads(repository.files[repository.main][MANIFEST])["version"] == "0.3.1"


def test_interrupted_publication_recovers_original_merge_after_main_advances(repository):
    Maintenance(repository).run()
    repository.complete_validation()
    repository.fail_release_dispatch_once = True
    with pytest.raises(PolicyError, match="after immutable tag creation"):
        Maintenance(repository).run()
    release_commit = repository.main
    advanced = "5" * 40
    repository.files[advanced] = {
        **repository.files[release_commit],
        "custom_components/lanbon_mqtt/light.py": "new_untested_runtime = True\n",
    }
    repository.parents[advanced] = [release_commit]
    repository.main = advanced
    before = len(repository.writes)
    Maintenance(repository).run()
    assert repository.writes[before:] == [
        ("POST", "/actions/workflows/release.yml/dispatches", {"ref": TAG})
    ]
    assert repository.tags[TAG] == {"type": "commit", "sha": release_commit}
    assert repository.release_runs[0]["head_sha"] == release_commit
    assert repository.main == advanced
    assert repository.files[release_commit]["custom_components/lanbon_mqtt/light.py"] == (
        "unchanged_device_runtime = True\n"
    )


def test_main_advancing_after_validation_stops_before_merge_mutation(repository, monkeypatch):
    Maintenance(repository).run()
    repository.complete_validation()
    original_request = repository.request
    main_reads = 0

    def concurrent_advance(method, path, data=None, optional=False):
        nonlocal main_reads
        if method == "GET" and path == "/git/ref/heads/main":
            main_reads += 1
            if main_reads == 3:
                advanced = "5" * 40
                repository.files[advanced] = copy.deepcopy(repository.files[MAIN])
                repository.parents[advanced] = [MAIN]
                repository.main = advanced
        return original_request(method, path, data, optional)

    monkeypatch.setattr(repository, "request", concurrent_advance)
    before = len(repository.writes)
    with pytest.raises(PolicyError, match="Main advanced before merge"):
        Maintenance(repository).run()
    assert main_reads == 3
    assert len(repository.writes) == before
    assert repository.prs[10]["state"] == "open"
    assert not repository.tags
