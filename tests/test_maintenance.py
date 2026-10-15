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


def test_disabled_pr_creation_stops_before_any_branch_or_commit_mutation():
    class DisabledAPI(ReadOnlyAPI):
        dry_run = False

        def request(self, method, path, data=None, optional=False):
            if path == "/actions/permissions/workflow":
                self.calls.append(path)
                return {"can_approve_pull_request_reviews": False}
            return super().request(method, path, data, optional)

    maintenance = Maintenance(DisabledAPI())
    with pytest.raises(PolicyError, match="pull-request creation is disabled"):
        maintenance.create_candidate("b" * 40, "0.3.0")
    assert maintenance.api.calls == [
        "/git/ref/heads/automation/release-0.3.1",
        "/actions/permissions/workflow",
    ]
