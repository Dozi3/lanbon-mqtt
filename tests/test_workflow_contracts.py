"""Release authority stays on main; tags and tested bytes gate publication."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def test_maintenance_definition_cannot_be_selected_from_a_branch():
    data = workflow("maintenance.yml")
    assert set(data["on"]) == {"schedule", "workflow_run", "repository_dispatch"}
    assert set(data["on"]["repository_dispatch"]["types"]) == {
        "lanbon-maintenance",
        "lanbon-maintenance-dry-run",
    }
    job = data["jobs"]["maintain"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["persist-credentials"] == "false"
    assert re.fullmatch(r"actions/checkout@[a-f0-9]{40}", checkout["uses"])


def test_maintenance_has_only_the_required_token_scopes():
    data = workflow("maintenance.yml")
    assert data["permissions"] == {"contents": "read"}
    assert data["jobs"]["maintain"]["permissions"] == {
        "actions": "write",
        "checks": "read",
        "contents": "write",
        "pull-requests": "write",
        "statuses": "read",
    }
    env = data["jobs"]["maintain"]["steps"][1]["env"]
    assert env["LANBON_RELEASE_PRS_ENABLED"] == "${{ vars.LANBON_RELEASE_PRS_ENABLED }}"


@pytest.mark.parametrize(
    "ref,accepted",
    [
        ("refs/tags/v0.3.0", True),
        ("refs/tags/v0.3.1-rc.4", True),
        ("refs/tags/v1.2.3-alpha1", True),
        ("refs/heads/main", False),
        ("refs/tags/v1.2.3-a..b", False),
        ("refs/tags/v1.2.3-alpha-beta", False),
        ("refs/tags/v1.2.3-", False),
        ("refs/tags/v1.2.3/x", False),
    ],
)
def test_release_tag_gate_matches_the_supported_package_version_format(ref, accepted):
    command = workflow("release.yml")["jobs"]["tag"]["steps"][0]["run"]
    pattern = command.split(" =~ ", 1)[1].removesuffix(" ]]")
    assert bool(re.fullmatch(pattern, ref)) is accepted


def test_publication_depends_on_tag_and_exact_artifact_validation():
    jobs = workflow("release.yml")["jobs"]
    assert jobs["validate"]["needs"] == "tag"
    assert jobs["validate"]["uses"] == "./.github/workflows/validate.yml"
    assert jobs["publish"]["needs"] == "validate"
    steps = jobs["publish"]["steps"]
    comparison = next(i for i, step in enumerate(steps) if "Compare release bytes" in step["name"])
    publication = next(
        i for i, step in enumerate(steps) if "publish_release.py" in step.get("run", "")
    )
    assert comparison < publication
    assert "sha256sum --check SHA256SUMS" in steps[comparison]["run"]
