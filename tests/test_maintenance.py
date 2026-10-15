"""Adversarial policy tests for unattended GitHub maintenance (no network writes)."""

import copy
import json

import pytest

from tools.maintenance import (
    ACTIONS_BOT,
    CHANGELOG,
    DEPENDABOT,
    MANIFEST,
    GitHub,
    Maintenance,
    PolicyError,
    check_ci,
    check_pr_identity,
    check_ruff_update,
    next_patch,
    release_changes,
    safe_paths,
    version,
)

REPO = "example/lanbon-mqtt"
SHA = "a" * 40


def project(pin="0.16.9"):
    return (
        '[project]\nname = "fixture"\ndependencies = []\n'
        f'[dependency-groups]\ndev = ["ruff=={pin}", "pytest==9.0.0"]\n'
    )


def lock(pin="0.16.9"):
    return f"""version = 1
revision = 3
[[package]]
name = "fixture"
version = "0.1.0"
source = {{ virtual = "." }}
[package.dev-dependencies]
dev = [{{ name = "ruff" }}, {{ name = "pytest" }}]
[package.metadata.requires-dev]
dev = [{{ name = "ruff", specifier = "=={pin}" }}, {{ name = "pytest", specifier = "==9.0.0" }}]
[[package]]
name = "pytest"
version = "9.0.0"
source = {{ registry = "https://pypi.org/simple" }}
[[package]]
name = "ruff"
version = "{pin}"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ url = "https://files.pythonhosted.org/packages/ruff-{pin}.tar.gz",
hash = "sha256:{"a" * 64}", size = 500 }}
wheels = [{{ url = "https://files.pythonhosted.org/packages/ruff-{pin}-py3-none-any.whl",
hash = "sha256:{"b" * 64}", size = 400 }}]
""".replace(",\nhash", ", hash")


def bot(identity):
    return {"login": identity[0], "id": identity[1], "type": "Bot"}


def pr(identity=DEPENDABOT):
    return {
        "number": 1,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "user": bot(identity),
        "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": REPO}},
        "head": {"ref": "dependabot/ruff", "sha": SHA, "repo": {"full_name": REPO}},
    }


def ci():
    checks = [
        {
            "name": name,
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
            "app": {"id": 15368, "slug": "github-actions"},
            "check_suite": {"id": 25},
        }
        for name in ("Tests, lint and package", "Hassfest", "HACS")
    ]
    runs = [
        {
            "head_sha": SHA,
            "name": "Validate",
            "path": ".github/workflows/validate.yml",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": REPO},
            "head_repository": {"full_name": REPO},
            "event": "workflow_dispatch",
            "check_suite_id": 25,
        }
    ]
    return checks, runs


def test_exact_patch_update_is_eligible():
    check_ruff_update(project(), project("0.16.10"), lock(), lock("0.16.10"))


@pytest.mark.parametrize("pin", ["0.17.0", "1.16.10", "0.16.9", "0.16.8", "0.16.10-rc.1"])
def test_minor_major_downgrade_unchanged_and_prerelease_require_review(pin):
    with pytest.raises(PolicyError):
        check_ruff_update(project(), project(pin), lock(), lock(pin))


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: text.replace("ruff==0.16.10", "ruff>=0.16.10"),
        lambda text: text.replace("dependencies = []", 'dependencies = ["evil==1.0.0"]'),
        lambda text: text.replace("pytest==9.0.0", "pytest==9.0.1"),
        lambda text: text + '\n[tool.uv]\nindex-url = "https://example.invalid"\n',
    ],
)
def test_project_tampering_is_rejected(transform):
    with pytest.raises(PolicyError):
        check_ruff_update(project(), transform(project("0.16.10")), lock(), lock("0.16.10"))


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: text.replace('version = "9.0.0"', 'version = "9.0.1"'),
        lambda text: text.replace('specifier = "==9.0.0"', 'specifier = "==9.0.1"'),
        lambda text: text.replace('virtual = "."', 'virtual = "elsewhere"'),
        lambda text: text.replace("revision = 3", "revision = 4"),
        lambda text: text.replace("files.pythonhosted.org", "evil.invalid"),
        lambda text: text.replace(
            'registry = "https://pypi.org/simple"', 'registry = "https://evil.invalid"'
        ),
        lambda text: text + '\ndependencies = [{ name = "evil" }]\n',
        lambda text: text.replace("sha256:" + "a" * 64, "sha256:bad"),
    ],
)
def test_nested_lock_and_supply_source_tampering_are_rejected(transform):
    with pytest.raises(PolicyError):
        check_ruff_update(project(), project("0.16.10"), lock(), transform(lock("0.16.10")))


@pytest.mark.parametrize("identity", [DEPENDABOT, ACTIONS_BOT])
def test_bot_identity_requires_login_numeric_identity_and_type(identity):
    value = pr(identity)
    check_pr_identity(value, REPO, identity)
    for key, replacement in (("login", "someone"), ("id", 123), ("type", "User")):
        changed = copy.deepcopy(value)
        changed["user"][key] = replacement
        with pytest.raises(PolicyError):
            check_pr_identity(changed, REPO, identity)


@pytest.mark.parametrize(
    ("key", "value"),
    [("state", "closed"), ("draft", True), ("mergeable", None), ("mergeable_state", "behind")],
)
def test_unready_pull_requests_are_rejected(key, value):
    changed = pr()
    changed[key] = value
    with pytest.raises(PolicyError):
        check_pr_identity(changed, REPO, DEPENDABOT)


def test_prevalidation_identity_allows_blocked_checks_but_never_weakens_merge_gate():
    value = pr()
    value.update(mergeable=None, mergeable_state="blocked")
    check_pr_identity(value, REPO, DEPENDABOT, ready=False)
    with pytest.raises(PolicyError, match="clean, unblocked merge"):
        check_pr_identity(value, REPO, DEPENDABOT)
    value["draft"] = True
    with pytest.raises(PolicyError, match="not ready"):
        check_pr_identity(value, REPO, DEPENDABOT, ready=False)


@pytest.mark.parametrize("side", ["base", "head"])
def test_foreign_repository_is_rejected(side):
    value = pr()
    value[side]["repo"]["full_name"] = "someone/fork"
    with pytest.raises(PolicyError):
        check_pr_identity(value, REPO, DEPENDABOT)


def test_exact_head_successful_workflow_and_checks_are_accepted():
    checks, runs = ci()
    check_ci(SHA, checks, [], runs, REPO)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("head_sha", "old"),
        ("status", "queued"),
        ("conclusion", "failure"),
        ("conclusion", "cancelled"),
        ("conclusion", "timed_out"),
        ("conclusion", "action_required"),
        ("conclusion", "skipped"),
        ("app", {"id": 123, "slug": "fake-actions"}),
        ("check_suite", {"id": 26}),
    ],
)
def test_missing_stale_failed_or_spoofed_required_checks_are_rejected(key, value):
    checks, runs = ci()
    checks[0][key] = value
    with pytest.raises(PolicyError):
        check_ci(SHA, checks, [], runs, REPO)


@pytest.mark.parametrize("status", ["queued", "in_progress", "completed"])
def test_unrelated_reviewbot_pending_or_failed_check_blocks_merge(status):
    checks, runs = ci()
    checks.append({"name": "Reviewer", "head_sha": SHA, "status": status, "conclusion": "failure"})
    with pytest.raises(PolicyError):
        check_ci(SHA, checks, [], runs, REPO)


@pytest.mark.parametrize("state", ["pending", "failure", "error"])
def test_legacy_status_cannot_be_ignored(state):
    checks, runs = ci()
    with pytest.raises(PolicyError):
        check_ci(SHA, checks, [{"state": state}], runs, REPO)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("head_sha", "old"),
        ("name", "Other"),
        ("path", ".github/workflows/forged.yml"),
        ("status", "queued"),
        ("conclusion", "failure"),
        ("repository", {"full_name": "someone/fork"}),
        ("head_repository", {"full_name": "someone/fork"}),
        ("event", "pull_request_target"),
        ("check_suite_id", 999),
    ],
)
def test_workflow_provenance_is_required(key, value):
    checks, runs = ci()
    runs[0][key] = value
    with pytest.raises(PolicyError):
        check_ci(SHA, checks, [], runs, REPO)


def test_absent_checks_or_workflow_cannot_approve():
    checks, runs = ci()
    for observed, workflows in (([], runs), (checks[:-1], runs), (checks, [])):
        with pytest.raises(PolicyError):
            check_ci(SHA, observed, [], workflows, REPO)


@pytest.mark.parametrize(
    "path",
    [
        MANIFEST,
        "custom_components/lanbon_mqtt/light.py",
        "tools/maintenance.py",
        "tests/test_maintenance.py",
        ".github/workflows/validate.yml",
        "hacs.json",
        "docs/../tools/maintenance.py",
        "docs//bad.md",
    ],
)
def test_runtime_automation_or_ambiguous_paths_require_human_review(path):
    with pytest.raises(PolicyError):
        safe_paths([{"filename": path, "status": "modified"}])


def test_safe_docs_paths_are_accepted_but_empty_or_renames_are_not():
    safe_paths(
        [
            {"filename": path, "status": "modified"}
            for path in ("README.md", "docs/images/demo.jpg", "pyproject.toml", "uv.lock")
        ]
    )
    for files in ([], [{"filename": "docs/readme.md", "status": "renamed"}]):
        with pytest.raises(PolicyError):
            safe_paths(files)


def test_generated_release_changes_are_deterministic_and_preserve_runtime_metadata():
    manifest = '{\n  "domain": "lanbon_mqtt",\n  "version": "0.3.0",\n  "other": 3\n}\n'
    changelog = "# Changelog\n\nExisting history.\n"
    changes = release_changes(manifest, changelog, "0.3.0")
    assert changes == release_changes(manifest, changelog, "0.3.0")
    assert set(changes) == {MANIFEST, CHANGELOG}
    assert changes[MANIFEST] == manifest.replace('"0.3.0"', '"0.3.1"')
    assert changes[CHANGELOG].startswith("# Changelog\n\n## 0.3.1\n")
    assert changes[CHANGELOG].endswith("\nExisting history.\n")
    parsed = json.loads(changes[MANIFEST])
    parsed["version"] = "0.3.0"
    assert parsed == json.loads(manifest)


@pytest.mark.parametrize("value", ["0.3.0-rc.4", "v0.3.0", "00.3.0", "0.3", "0.3.0+meta", None])
def test_release_version_parser_requires_plain_stable_semver(value):
    with pytest.raises(PolicyError):
        version(value)


def test_patch_release_does_not_jump_minor_or_accept_mismatched_manifest():
    assert next_patch("0.3.9") == "0.3.10"
    with pytest.raises(PolicyError):
        release_changes('{"version":"0.4.0"}', "# Changelog\n", "0.3.0")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_dry_run_api_rejects_every_mutation_before_network(method, monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("Dry run attempted a network write")

    monkeypatch.setattr("urllib.request.urlopen", network_forbidden)
    api = GitHub(REPO, "test-token", dry_run=True)
    with pytest.raises(PolicyError, match="Dry-run write blocked"):
        api.request(method, "/git/refs", {"ref": "refs/tags/v0.3.1"})


class ReadOnlyAPI:
    """Independent API seam: any mutation, including dispatch, fails the test."""

    dry_run = True
    repo = REPO

    def __init__(self, *, changed=True, runs=None):
        self.changed = changed
        self.runs = runs or []
        self.calls = []

    def request(self, method, path, data=None, optional=False):
        assert method == "GET", (method, path, data)
        self.calls.append(path)
        if path == "":
            return {"default_branch": "main"}
        if path == "/releases/latest":
            return {"tag_name": "v0.3.0", "prerelease": False, "draft": False}
        if path == "/commits/v0.3.0":
            return {"sha": "stable"}
        if path == "/git/ref/heads/main":
            return {"object": {"sha": "b" * 40}}
        if path.startswith("/git/ref/heads/automation/release-"):
            return None
        raise AssertionError(path)

    def compare(self, base, head):
        if not self.changed:
            return []
        return [{"filename": "docs/usage.md", "status": "modified"}]

    def content(self, path, ref):
        if path == MANIFEST:
            return '{"version": "0.3.0"}\n'
        if path == CHANGELOG:
            return "# Changelog\n\nOld notes.\n"
        raise AssertionError(path)

    def pages(self, path, key=None):
        self.calls.append(path)
        if path.startswith("/pulls?"):
            return []
        if "workflows/validate.yml/runs" in path:
            return self.runs
        raise AssertionError(path)


def test_complete_dry_run_can_plan_candidate_without_any_mutation():
    maintenance = Maintenance(ReadOnlyAPI())
    maintenance.run()
    assert maintenance.notes == ["Dry run: would create or recover release PR for 0.3.1"]


def test_no_release_loop_when_main_matches_stable():
    maintenance = Maintenance(ReadOnlyAPI(changed=False))
    maintenance.run()
    assert "no new release" in maintenance.notes[0]


def test_dry_run_merge_does_not_merge_or_dispatch():
    maintenance = Maintenance(ReadOnlyAPI())
    assert maintenance.merge(pr()) is None
    assert maintenance.notes[0].startswith("Dry run: would squash merge PR #1")


def test_missing_validation_dry_run_plans_one_dispatch_without_writes():
    maintenance = Maintenance(ReadOnlyAPI())
    assert maintenance.approved(pr(), "b" * 40) is False
    assert maintenance.notes[0].startswith("Dry run: would dispatch missing Validate run")


def test_stale_base_blocks_before_ci_or_merge():
    maintenance = Maintenance(ReadOnlyAPI())
    with pytest.raises(PolicyError, match="base snapshot is stale"):
        maintenance.approved(pr(), "new-main")
    assert not maintenance.api.calls


def test_generated_candidate_rejects_extra_runtime_file():
    class TamperedAPI(ReadOnlyAPI):
        def compare(self, base, head):
            return [
                {"filename": MANIFEST, "status": "modified"},
                {"filename": CHANGELOG, "status": "modified"},
                {"filename": "custom_components/lanbon_mqtt/light.py", "status": "modified"},
            ]

    with pytest.raises(PolicyError, match="beyond generated"):
        Maintenance(TamperedAPI()).check_generated("main", SHA, "0.3.0")


def test_generated_candidate_rejects_manifest_parameter_tampering():
    class TamperedAPI(ReadOnlyAPI):
        def compare(self, base, head):
            return [{"filename": name, "status": "modified"} for name in (MANIFEST, CHANGELOG)]

        def content(self, path, ref):
            if path == MANIFEST and ref == SHA:
                return '{"version": "0.3.1", "evil": true}\n'
            return super().content(path, ref)

    with pytest.raises(PolicyError, match="Unexpected generated"):
        Maintenance(TamperedAPI()).check_generated("main", SHA, "0.3.0")


def test_api_compare_rejects_truncated_or_diverged_results(monkeypatch):
    api = GitHub(REPO, "test-token", dry_run=True)
    for payload in (
        {"behind_by": 1, "status": "diverged", "files": []},
        {"behind_by": 0, "status": "ahead", "files": [{}] * 300},
        {"behind_by": 0, "status": "diverged", "files": []},
    ):
        monkeypatch.setattr(api, "request", lambda *args, value=payload: value)
        with pytest.raises(PolicyError):
            api.compare("main", SHA)


@pytest.mark.parametrize("setting", [None, "false", "TRUE"])
def test_disabled_pr_creation_stops_before_any_branch_or_commit_mutation(setting, monkeypatch):
    if setting is None:
        monkeypatch.delenv("LANBON_RELEASE_PRS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LANBON_RELEASE_PRS_ENABLED", setting)

    class DisabledAPI(ReadOnlyAPI):
        dry_run = False

    maintenance = Maintenance(DisabledAPI())
    with pytest.raises(PolicyError, match="pull-request creation is disabled"):
        maintenance.create_candidate("b" * 40, "0.3.0")
    assert maintenance.api.calls == ["/git/ref/heads/automation/release-0.3.1"]


def test_explicit_release_pr_opt_in_permits_expected_creation_and_dispatch(monkeypatch):
    monkeypatch.setenv("LANBON_RELEASE_PRS_ENABLED", "true")

    class EnabledAPI(ReadOnlyAPI):
        dry_run = False

        def __init__(self):
            super().__init__()
            self.writes = []

        def request(self, method, path, data=None, optional=False):
            if method == "POST":
                self.writes.append((path, data))
                return {"sha": "created", "number": 10}
            if path == f"/git/commits/{'b' * 40}":
                return {"tree": {"sha": "original-tree"}}
            return super().request(method, path, data, optional)

    maintenance = Maintenance(EnabledAPI())
    maintenance.create_candidate("b" * 40, "0.3.0")
    assert [path for path, _ in maintenance.api.writes] == [
        "/git/trees",
        "/git/commits",
        "/git/refs",
        "/pulls",
        "/actions/workflows/validate.yml/dispatches",
    ]
    assert maintenance.api.writes[-1][1] == {"ref": "automation/release-0.3.1"}
    assert all("permissions" not in path for path in maintenance.api.calls)


@pytest.mark.parametrize("validate_main", [True, False])
def test_release_candidate_merge_can_skip_redundant_main_validation(validate_main):
    class MergeAPI(ReadOnlyAPI):
        dry_run = False

        def __init__(self):
            super().__init__()
            self.writes = []

        def request(self, method, path, data=None, optional=False):
            if method == "GET" and path == "/git/ref/heads/main":
                return {"object": {"sha": "b" * 40}}
            self.writes.append((method, path, data))
            if method == "PUT" and path == "/pulls/1/merge":
                return {"merged": True, "sha": SHA}
            assert method == "POST" and path == "/actions/workflows/validate.yml/dispatches"

    maintenance = Maintenance(MergeAPI())
    assert maintenance.merge(pr(), validate_main=validate_main) == SHA
    assert maintenance.api.writes[0] == (
        "PUT",
        "/pulls/1/merge",
        {"sha": SHA, "merge_method": "squash"},
    )
    assert len(maintenance.api.writes) == (2 if validate_main else 1)


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested"])
def test_running_exact_tag_release_is_not_dispatched_twice(status):
    class PublishingAPI(ReadOnlyAPI):
        dry_run = False

        def request(self, method, path, data=None, optional=False):
            assert method == "GET", "An existing release run must prevent every write"
            if path == f"/commits/{SHA}":
                return {"parents": [{"sha": "parent"}]}
            if path == "/releases/tags/v0.3.1":
                return None
            if path == "/git/ref/tags/v0.3.1":
                return {"object": {"sha": SHA, "type": "commit"}}
            raise AssertionError(path)

        def compare(self, base, head):
            if head == SHA:
                return [{"filename": name, "status": "modified"} for name in (MANIFEST, CHANGELOG)]
            return [{"filename": "docs/usage.md", "status": "modified"}]

        def content(self, path, ref):
            if ref == SHA:
                return release_changes(
                    super().content(MANIFEST, "parent"),
                    super().content(CHANGELOG, "parent"),
                    "0.3.0",
                )[path]
            return super().content(path, ref)

        def pages(self, path, key=None):
            if path == f"/commits/{SHA}/pulls":
                value = pr(ACTIONS_BOT)
                value.update(
                    state="closed",
                    merged_at="2026-10-15T12:00:00Z",
                    merge_commit_sha=SHA,
                    body=Maintenance.body("0.3.1"),
                )
                value["head"]["ref"] = "automation/release-0.3.1"
                return [value]
            if path == f"/actions/workflows/release.yml/runs?head_sha={SHA}":
                return [
                    {
                        "head_sha": SHA,
                        "head_branch": "v0.3.1",
                        "event": "workflow_dispatch",
                        "status": status,
                    }
                ]
            raise AssertionError(path)

    maintenance = Maintenance(PublishingAPI())
    maintenance.publish(SHA, "stable", "0.3.0")
    assert maintenance.notes == ["Release v0.3.1 is awaiting an existing release.yml run"]


@pytest.mark.parametrize(
    "kind", ["draft", "prerelease", "published_incomplete", "published_complete", "bad_tag"]
)
def test_draft_retries_and_published_release_integrity_gates(kind, monkeypatch):
    class DraftReleaseAPI(ReadOnlyAPI):
        dry_run = False

        def __init__(self):
            super().__init__()
            self.writes = []

        def request(self, method, path, data=None, optional=False):
            if method != "GET":
                self.writes.append((method, path, data))
                return None
            if path == f"/commits/{SHA}":
                return {"parents": [{"sha": "parent"}]}
            if path == "/releases/tags/v0.3.1":
                return {
                    "draft": kind == "draft",
                    "prerelease": kind == "prerelease",
                    "assets": [
                        {"name": name, "state": "uploaded", "size": 100}
                        for name in ("lanbon-mqtt-0.3.1.zip", "SHA256SUMS", "release-receipt.json")
                    ]
                    if kind in {"published_complete", "bad_tag"}
                    else [],
                }
            if path == "/git/ref/tags/v0.3.1":
                return {"object": {"sha": "wrong" if kind == "bad_tag" else SHA, "type": "commit"}}
            raise AssertionError(path)

        def pages(self, path, key=None):
            if path == f"/commits/{SHA}/pulls":
                value = pr(ACTIONS_BOT)
                value.update(
                    state="closed",
                    merged_at="2026-10-15T12:00:00Z",
                    merge_commit_sha=SHA,
                    body=Maintenance.body("0.3.1"),
                )
                value["head"]["ref"] = "automation/release-0.3.1"
                return [value]
            if path == f"/actions/workflows/release.yml/runs?head_sha={SHA}":
                return [
                    {
                        "head_sha": SHA,
                        "head_branch": "v0.3.1",
                        "event": "workflow_dispatch",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            raise AssertionError(path)

    maintenance = Maintenance(DraftReleaseAPI())
    # Other tests exercise these source policy gates; isolate draft recovery here.
    monkeypatch.setattr(maintenance, "safe_changes", lambda *args: None)
    monkeypatch.setattr(maintenance, "check_generated", lambda *args: None)
    if kind == "draft":
        maintenance.publish(SHA, "stable", "0.3.0")
        assert maintenance.api.writes == [
            ("POST", "/actions/workflows/release.yml/dispatches", {"ref": "v0.3.1"})
        ]
    elif kind == "published_complete":
        maintenance.publish(SHA, "stable", "0.3.0")
        assert maintenance.notes == ["Stable release v0.3.1 already exists; nothing to publish"]
        assert not maintenance.api.writes
    else:
        with pytest.raises(PolicyError):
            maintenance.publish(SHA, "stable", "0.3.0")
        assert not maintenance.api.writes


def test_main_advance_immediately_before_merge_blocks_every_write():
    value = pr()
    value["base"]["sha"] = "old-main"
    maintenance = Maintenance(ReadOnlyAPI())
    with pytest.raises(PolicyError, match="Main advanced before merge"):
        maintenance.merge(value)
    assert maintenance.api.calls == ["/git/ref/heads/main"]


def test_later_main_commit_recovers_publication_at_exact_merged_release_sha(monkeypatch):
    class AdvancedMainAPI(ReadOnlyAPI):
        def content(self, path, ref):
            if path == MANIFEST and ref == "b" * 40:
                return '{"version":"0.3.1"}'
            return super().content(path, ref)

        def pages(self, path, key=None):
            assert path == "/pulls?state=closed&base=main"
            value = pr(ACTIONS_BOT)
            value.update(
                state="closed",
                merged_at="2026-10-15T12:00:00Z",
                merge_commit_sha=SHA,
                body=Maintenance.body("0.3.1"),
            )
            value["head"]["ref"] = "automation/release-0.3.1"
            return [value]

        def compare(self, base, head):
            assert (base, head) == (SHA, "b" * 40)
            return [{"filename": "docs/usage.md", "status": "modified"}]

    maintenance = Maintenance(AdvancedMainAPI())
    publications = []
    monkeypatch.setattr(maintenance, "publish", lambda *args: publications.append(args))
    maintenance.run()
    assert publications == [(SHA, "stable", "0.3.0")]


class StaleGeneratedAPI(ReadOnlyAPI):
    dry_run = False

    def __init__(self, *, tampered=False):
        super().__init__()
        self.tampered = tampered
        self.writes = []

    def request(self, method, path, data=None, optional=False):
        if method != "GET":
            self.writes.append((method, path, data))
            return {"number": 10}
        if path == f"/compare/{'b' * 40}...{SHA}":
            return {"merge_base_commit": {"sha": "c" * 40}}
        if path == "/git/ref/heads/automation/release-0.3.1":
            return {"object": {"sha": SHA}}
        if path == f"/commits/{SHA}":
            return {"parents": [{"sha": "c" * 40}]}
        return super().request(method, path, data, optional)

    def compare(self, base, head):
        if base == "b" * 40 and head == SHA:
            raise PolicyError("Head is behind main")
        if base == "c" * 40 and head == SHA:
            return [{"filename": name, "status": "modified"} for name in (MANIFEST, CHANGELOG)]
        assert (base, head) == ("c" * 40, "b" * 40)
        return [{"filename": "docs/usage.md", "status": "modified"}]

    def content(self, path, ref):
        if ref == SHA:
            if self.tampered and path == MANIFEST:
                return '{"version":"0.3.1", "unreviewed":true}'
            return release_changes(
                super().content(MANIFEST, "c" * 40),
                super().content(CHANGELOG, "c" * 40),
                "0.3.0",
            )[path]
        return super().content(path, ref)


def release_pr():
    value = pr(ACTIONS_BOT)
    value["head"]["ref"] = "automation/release-0.3.1"
    value["body"] = Maintenance.body("0.3.1")
    value["mergeable_state"] = "blocked"
    return value


def test_stale_generated_pr_uses_native_expected_head_update_and_revalidation():
    maintenance = Maintenance(StaleGeneratedAPI())
    assert maintenance.candidate(release_pr(), "b" * 40, "0.3.0") is False
    assert maintenance.api.writes == [
        ("PUT", "/pulls/1/update-branch", {"expected_head_sha": SHA}),
        ("POST", "/actions/workflows/validate.yml/dispatches", {"ref": "automation/release-0.3.1"}),
    ]
    assert "awaiting fresh validation" in maintenance.notes[-1]


def test_stale_generated_pr_dry_run_cannot_update_or_dispatch():
    api = StaleGeneratedAPI()
    api.dry_run = True
    maintenance = Maintenance(api)
    assert maintenance.candidate(release_pr(), "b" * 40, "0.3.0") is False
    assert not api.writes


def test_stale_tampered_pr_cannot_update_or_dispatch():
    maintenance = Maintenance(StaleGeneratedAPI(tampered=True))
    with pytest.raises(PolicyError, match="Unexpected generated"):
        maintenance.candidate(release_pr(), "b" * 40, "0.3.0")
    assert not maintenance.api.writes


def test_proven_stale_orphan_branch_is_recovered_without_rewriting_refs(monkeypatch):
    monkeypatch.setenv("LANBON_RELEASE_PRS_ENABLED", "true")
    maintenance = Maintenance(StaleGeneratedAPI())
    maintenance.create_candidate("b" * 40, "0.3.0")
    assert [path for _, path, _ in maintenance.api.writes] == [
        "/pulls",
        "/actions/workflows/validate.yml/dispatches",
    ]
    assert "proven generated orphan branch" in maintenance.notes[0]


def test_tampered_orphan_branch_requires_review_without_writes(monkeypatch):
    monkeypatch.setenv("LANBON_RELEASE_PRS_ENABLED", "true")
    maintenance = Maintenance(StaleGeneratedAPI(tampered=True))
    with pytest.raises(PolicyError, match="Unexpected generated"):
        maintenance.create_candidate("b" * 40, "0.3.0")
    assert not maintenance.api.writes


class ApprovalRunAPI(ReadOnlyAPI):
    dry_run = False

    def __init__(self, runs, *, checks=None, statuses=None):
        super().__init__()
        self.runs = runs
        self.checks = ci()[0] if checks is None else checks
        self.statuses = [] if statuses is None else statuses
        self.writes = []

    def pages(self, path, key=None):
        if "workflows/validate.yml/runs" in path:
            return self.runs
        if "check-runs" in path:
            return self.checks
        if path.endswith("/statuses"):
            return self.statuses
        raise AssertionError(path)

    def request(self, method, path, data=None, optional=False):
        if method != "GET":
            self.writes.append((method, path, data))
            return None
        if path == "/pulls/1":
            return pr()
        if path == "/actions/workflows/validate.yml":
            return {"id": 321}
        return super().request(method, path, data, optional)


def validation_run(
    identifier, event="workflow_dispatch", status="completed", conclusion="success", *, attempt=1
):
    run = ci()[1][0]
    run.update(
        id=identifier,
        event=event,
        status=status,
        conclusion=conclusion,
        actor=bot(DEPENDABOT),
        pull_requests=[{"number": 1}],
        workflow_id=321,
        run_attempt=attempt,
    )
    return run


def test_original_approval_gated_run_is_retried_even_after_newer_successful_dispatch():
    api = ApprovalRunAPI(
        [validation_run(300), validation_run(200, "pull_request", conclusion="action_required")]
    )
    maintenance = Maintenance(api)
    assert maintenance.approved(pr(), "b" * 40) is False
    assert api.writes == [("POST", "/actions/runs/200/rerun", {})]
    assert "original Validate run 200 rerun" in maintenance.notes[-1]


@pytest.mark.parametrize("state", ["queued", "in_progress", "waiting", "completed"])
def test_newer_dispatch_does_not_justify_another_native_rerun(state):
    api = ApprovalRunAPI(
        [
            validation_run(100),
            validation_run(200, "pull_request", conclusion="action_required", attempt=2),
            validation_run(201, status=state, conclusion=None),
        ]
    )
    with pytest.raises(PolicyError, match="human action is required"):
        Maintenance(api).approved(pr(), "b" * 40)
    assert not api.writes


def test_newer_failed_dispatch_does_not_justify_another_native_rerun():
    api = ApprovalRunAPI(
        [
            validation_run(200, "pull_request", conclusion="action_required", attempt=2),
            validation_run(201, conclusion="failure"),
        ]
    )
    with pytest.raises(PolicyError, match="human action is required"):
        Maintenance(api).approved(pr(), "b" * 40)
    assert not api.writes


def test_natural_approval_only_run_retries_original_run():
    api = ApprovalRunAPI([validation_run(200, "pull_request", conclusion="action_required")])
    assert Maintenance(api).approved(pr(), "b" * 40) is False
    assert len(api.writes) == 1
    assert api.writes[0] == ("POST", "/actions/runs/200/rerun", {})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", {"full_name": "foreign/repo"}),
        ("head_repository", {"full_name": "foreign/repo"}),
        ("path", ".github/workflows/untrusted.yml"),
        ("head_sha", "f" * 40),
        ("name", "Untrusted workflow"),
    ],
)
def test_foreign_or_stale_approval_run_cannot_justify_refresh(field, value):
    untrusted = validation_run(200, "pull_request", conclusion="action_required")
    untrusted[field] = value
    api = ApprovalRunAPI([validation_run(100), untrusted])
    assert Maintenance(api).approved(pr(), "b" * 40) is True
    assert not api.writes


def test_approval_gated_dry_run_does_not_retry_dispatch_or_approve():
    api = ApprovalRunAPI(
        [validation_run(100), validation_run(200, "pull_request", conclusion="action_required")]
    )
    api.dry_run = True
    maintenance = Maintenance(api)
    assert maintenance.approved(pr(), "b" * 40) is False
    assert not api.writes
    assert maintenance.notes[-1] == "Dry run: would rerun original approval-gated Validate run 200"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", {"login": "dependabot[bot]", "id": 123, "type": "Bot"}),
        ("actor", {"login": "other[bot]", "id": 49699333, "type": "Bot"}),
        ("actor", {"login": "dependabot[bot]", "id": 49699333, "type": "User"}),
        ("pull_requests", [{"number": 2}]),
        ("pull_requests", []),
        ("workflow_id", 999),
        ("id", 0),
    ],
)
def test_native_rerun_requires_original_bot_workflow_and_pull_request_provenance(field, value):
    run = validation_run(200, "pull_request", conclusion="action_required")
    run[field] = value
    api = ApprovalRunAPI([run])
    with pytest.raises(PolicyError, match="provenance could not be proved"):
        Maintenance(api).approved(pr(), "b" * 40)
    assert not api.writes


@pytest.mark.parametrize("attempt", [None, 0, 2, 3])
def test_native_rerun_never_retries_an_unknown_or_already_retried_attempt(attempt):
    run = validation_run(200, "pull_request", conclusion="action_required", attempt=attempt)
    api = ApprovalRunAPI([run])
    with pytest.raises(PolicyError, match="human action is required"):
        Maintenance(api).approved(pr(), "b" * 40)
    assert not api.writes


def test_actual_failed_validation_still_blocks_without_rerun():
    api = ApprovalRunAPI([validation_run(201, conclusion="failure")])
    with pytest.raises(PolicyError, match="Latest executable Validate run failed"):
        Maintenance(api).approved(pr(), "b" * 40)
    assert not api.writes


def test_normal_running_validation_is_a_quiet_wait():
    api = ApprovalRunAPI([validation_run(200, "pull_request", "in_progress", None)])
    assert Maintenance(api).approved(pr(), "b" * 40) is False
    assert not api.writes


def test_pending_review_check_waits_without_red_job_or_merge():
    checks = ci()[0]
    checks.append({"name": "Reviewer", "head_sha": SHA, "status": "queued", "conclusion": None})
    api = ApprovalRunAPI([validation_run(100)], checks=checks)
    assert Maintenance(api).approved(pr(), "b" * 40) is False
    assert not api.writes


def test_completed_review_failure_still_blocks_while_another_check_is_pending():
    checks = ci()[0]
    checks.extend(
        [
            {"name": "Reviewer", "head_sha": SHA, "status": "completed", "conclusion": "failure"},
            {"name": "Other", "head_sha": SHA, "status": "queued", "conclusion": None},
        ]
    )
    api = ApprovalRunAPI([validation_run(100)], checks=checks)
    with pytest.raises(PolicyError, match="A completed check failed"):
        Maintenance(api).approved(pr(), "b" * 40)
    assert not api.writes


@pytest.mark.parametrize("suffix", ["", "\n\n## Summary by Sourcery\n\n- Documentation update.\n"])
def test_canonical_release_body_accepts_only_known_app_summary_append(suffix):
    canonical = Maintenance.body("0.3.1")
    assert Maintenance.matches_body(canonical + suffix, "0.3.1")


@pytest.mark.parametrize(
    "body",
    [
        Maintenance.body("0.3.1").replace("maintenance-release:v1", "maintenance-release:v2"),
        Maintenance.body("0.3.1").replace(
            "runtime changes require human review", "automatic runtime changes"
        ),
        Maintenance.body("0.3.1") + "\n\n## Untrusted append\n\nText.",
        Maintenance.body("0.3.1") + "\n\n## Summary by Sourcery\nUnrecognized heading delimiter.",
        Maintenance.body("0.3.1")[:-1],
        Maintenance.body("0.3.2"),
        None,
    ],
)
def test_changed_truncated_or_unrecognized_release_body_cannot_match(body):
    assert not Maintenance.matches_body(body, "0.3.1")


def test_known_summary_append_also_preserves_merged_release_recovery_identity():
    value = release_pr()
    value.update(state="closed", merged_at="2026-10-15T12:00:00Z", merge_commit_sha=SHA)
    value["body"] += "\n\n## Summary by Sourcery\n\n- Release notes.\n"
    assert Maintenance(ReadOnlyAPI()).merged_release_pr(value, "0.3.1")
    value["user"]["id"] = 123
    assert not Maintenance(ReadOnlyAPI()).merged_release_pr(value, "0.3.1")
