"""Exercise release retries against real archives/git and a fake GitHub CLI boundary."""

import hashlib
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from tools.publish_release import PublicationError, publish


class GitHubCLI:
    """Represent remote draft/public state; no request reaches GitHub."""

    def __init__(self, tag, head):
        self.tag, self.head = tag, head
        self.release = None
        self.assets = {}
        self.calls = []
        self.fail = None
        self.corrupt_download = False
        self.latest_tag = "v0.2.0"
        self.last_edit = None

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        operation = args[2] if args[1] == "release" else "api"
        if operation == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "Simulated network failure")
        if operation == "api":
            output = self.latest_tag if args[2].endswith("/releases/latest") else self.head
        elif operation == "view":
            if self.release is None:
                return subprocess.CompletedProcess(args, 1, "", "release not found")
            output = json.dumps({**self.release, "assets": [{"name": n} for n in self.assets]})
        elif operation == "create":
            assert "--draft" in args and "--verify-tag" in args
            self.release = {
                "tagName": self.tag,
                "isDraft": True,
                "isPrerelease": "--prerelease" in args,
            }
            output = "created"
        elif operation == "upload":
            assert self.release["isDraft"], "Publisher tried overwriting public assets"
            assert args[-1] == "--clobber"
            self.assets.update({Path(p).name: Path(p).read_bytes() for p in args[4:-1]})
            output = "uploaded"
        elif operation == "download":
            destination = Path(args[args.index("--dir") + 1])
            for name, data in self.assets.items():
                (destination / name).write_bytes(
                    data + (b"changed" if self.corrupt_download else b"")
                )
            output = "downloaded"
        elif operation == "edit":
            self.last_edit = args
            self.release["isDraft"] = "--draft=false" not in args
            self.release["isPrerelease"] = "--prerelease=true" in args
            output = "published"
        else:
            pytest.fail(f"Unexpected command {args}")
        return subprocess.CompletedProcess(args, 0, output, "")

    @property
    def writes(self):
        return [call for call in self.calls if call[2] in {"create", "upload", "edit"}]


@pytest.fixture
def release_environment(tmp_path, monkeypatch):
    def setup(tag="v0.3.0"):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GH_REPO", "example/lanbon-mqtt")
        real_run = subprocess.run

        def git(*args):
            return real_run(
                ["git", *args], check=True, capture_output=True, text=True
            ).stdout.strip()

        git("init", "--quiet")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "--quiet",
            "-m",
            "fixture",
        )
        git("tag", tag)
        remote = GitHubCLI(tag, git("rev-parse", "HEAD"))

        def boundary(args, **kwargs):
            return remote.run(args, **kwargs) if args[0] == "gh" else real_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", boundary)
        dist = tmp_path / "dist"
        dist.mkdir()
        archive = dist / f"lanbon-mqtt-{tag[1:]}.zip"
        members = {
            "LICENSE": b"MIT fixture\n",
            "custom_components/lanbon_mqtt/manifest.json": json.dumps(
                {"version": tag[1:]}
            ).encode(),
        }
        with ZipFile(archive, "w") as saved:
            for name, data in members.items():
                saved.writestr(name, data)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (dist / "SHA256SUMS").write_bytes(f"{digest}  {archive.name}\n".encode())
        (dist / "release-receipt.json").write_text(
            json.dumps(
                {
                    "version": tag[1:],
                    "archive": archive.name,
                    "sha256": digest,
                    "bytes": archive.stat().st_size,
                    "file_count": len(members),
                    "member_sha256": {n: hashlib.sha256(d).hexdigest() for n, d in members.items()},
                }
            ),
            encoding="utf-8",
        )
        return tag, dist, remote

    return setup


def existing(remote, dist, *, draft):
    remote.release = {"tagName": remote.tag, "isDraft": draft, "isPrerelease": "-" in remote.tag}
    remote.assets = {path.name: path.read_bytes() for path in dist.iterdir()}


@pytest.mark.parametrize("tag", ["v0.3.0", "v0.3.1-rc.1"])
def test_new_release_verified_before_publication_and_repeat_is_read_only(release_environment, tag):
    tag, dist, remote = release_environment(tag)
    publish(tag, dist)
    operations = [call[2] for call in remote.calls]
    assert operations.index("download") < operations.index("edit")
    assert remote.release == {"tagName": tag, "isDraft": False, "isPrerelease": "-" in tag}
    assert remote.assets == {path.name: path.read_bytes() for path in dist.iterdir()}
    remote.calls.clear()
    publish(tag, dist)
    assert not remote.writes


def test_partial_draft_resumes_and_replaces_bad_unpublished_asset(release_environment):
    tag, dist, remote = release_environment()
    existing(remote, dist, draft=True)
    remote.assets = {"SHA256SUMS": b"incomplete previous upload"}
    publish(tag, dist)
    assert not remote.release["isDraft"]
    assert [call[2] for call in remote.writes] == ["upload", "edit"]


@pytest.mark.parametrize("change", ["bytes", "extra", "missing", "prerelease"])
def test_published_release_mismatch_never_overwrites(release_environment, change):
    tag, dist, remote = release_environment()
    existing(remote, dist, draft=False)
    if change == "bytes":
        remote.assets["SHA256SUMS"] = b"different"
    elif change == "extra":
        remote.assets["unexpected.txt"] = b"extra"
    elif change == "missing":
        del remote.assets["SHA256SUMS"]
    else:
        remote.release["isPrerelease"] = True
    with pytest.raises(PublicationError):
        publish(tag, dist)
    assert not remote.writes


def test_draft_with_unexpected_asset_requires_human_review(release_environment):
    tag, dist, remote = release_environment()
    existing(remote, dist, draft=True)
    remote.assets["unexpected.txt"] = b"review"
    with pytest.raises(PublicationError, match="unexpected assets"):
        publish(tag, dist)
    assert not remote.writes
    assert remote.release["isDraft"]


@pytest.mark.parametrize("failure", ["upload", "download", "edit", "corrupt"])
def test_failure_leaves_draft_and_next_attempt_recovers(release_environment, failure):
    tag, dist, remote = release_environment()
    remote.fail = failure if failure != "corrupt" else None
    remote.corrupt_download = failure == "corrupt"
    with pytest.raises(PublicationError):
        publish(tag, dist)
    assert remote.release["isDraft"]
    remote.fail = None
    remote.corrupt_download = False
    publish(tag, dist)
    assert not remote.release["isDraft"]


@pytest.mark.parametrize("field", ["version", "archive", "sha256", "bytes", "file_count"])
def test_receipt_mismatch_fails_before_remote_writes(release_environment, field):
    tag, dist, remote = release_environment()
    path = dist / "release-receipt.json"
    receipt = json.loads(path.read_text())
    receipt[field] = -1 if isinstance(receipt[field], int) else "wrong"
    path.write_text(json.dumps(receipt))
    with pytest.raises(PublicationError):
        publish(tag, dist)
    assert not remote.calls


def test_local_tag_mismatch_fails_before_remote_access(release_environment):
    tag, dist, remote = release_environment()
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "--quiet",
            "-m",
            "new head",
        ],
        check=True,
    )
    with pytest.raises(PublicationError, match="Local tag"):
        publish(tag, dist)
    assert not remote.calls


def test_remote_tag_mismatch_never_mutates(release_environment):
    tag, dist, remote = release_environment()
    remote.head = "0" * 40
    with pytest.raises(PublicationError, match="Remote tag"):
        publish(tag, dist)
    assert not remote.writes


def test_view_network_error_does_not_guess_release_is_missing(release_environment):
    tag, dist, remote = release_environment()
    remote.fail = "view"
    with pytest.raises(PublicationError, match="Cannot inspect"):
        publish(tag, dist)
    assert not remote.writes


@pytest.mark.parametrize(
    ("tag", "latest", "flag"),
    [
        ("v0.3.0", "v0.4.0", "false"),
        ("v0.3.0", "v0.2.0", "true"),
        ("v0.3.1-rc.1", "v0.2.0", "false"),
    ],
)
def test_older_stable_or_prerelease_never_displaces_latest(release_environment, tag, latest, flag):
    tag, dist, remote = release_environment(tag)
    remote.latest_tag = latest
    publish(tag, dist)
    assert f"--latest={flag}" in remote.last_edit
