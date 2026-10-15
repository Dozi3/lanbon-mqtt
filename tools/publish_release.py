"""Publish verified bytes through a resumable draft; never overwrite a public release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile


class PublicationError(ValueError):
    """A publication invariant could not be verified."""


def require(condition, message):
    if not condition:
        raise PublicationError(message)


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise PublicationError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result.stdout.strip()


def view(tag):
    result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "tagName,isDraft,isPrerelease,assets"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        if "release not found" in result.stderr.lower() or "HTTP 404" in result.stderr:
            return None
        raise PublicationError(f"Cannot inspect release (exit {result.returncode})")
    return json.loads(result.stdout)


def latest_flag(tag, repo):
    """A backfilled stable version must never displace a newer stable release."""
    if "-" in tag:
        return "--latest=false"
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        require("HTTP 404" in result.stderr, "Cannot inspect latest stable release")
        return "--latest=true"
    latest = result.stdout.strip()
    require(re.fullmatch(r"v\d+\.\d+\.\d+", latest), "Latest stable version is not comparable")
    newer = tuple(map(int, tag[1:].split("."))) >= tuple(map(int, latest[1:].split(".")))
    return f"--latest={str(newer).lower()}"


def package(tag, dist):
    require(
        re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?", tag),
        "Expected an exact version tag",
    )
    names = {f"lanbon-mqtt-{tag[1:]}.zip", "SHA256SUMS", "release-receipt.json"}
    require({path.name for path in dist.iterdir()} == names, "Unexpected distribution files")
    paths = {name: dist / name for name in names}
    require(
        all(path.is_file() and not path.is_symlink() for path in paths.values()),
        "Distribution files must be regular files",
    )
    receipt = json.loads(paths["release-receipt.json"].read_text(encoding="utf-8"))
    archive = paths[f"lanbon-mqtt-{tag[1:]}.zip"]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    require(
        receipt["version"] == tag[1:] and receipt["archive"] == archive.name,
        "Receipt version/archive does not match tag",
    )
    require(
        receipt["sha256"] == digest and receipt["bytes"] == archive.stat().st_size,
        "Archive digest/size does not match receipt",
    )
    require(
        paths["SHA256SUMS"].read_bytes() == f"{digest}  {archive.name}\n".encode(),
        "Checksum file does not match archive",
    )
    with ZipFile(archive) as saved:
        names = saved.namelist()
        require(saved.testzip() is None, "Archive CRC check failed")
        require(
            len(names) == len(set(names)) == receipt["file_count"]
            and set(names) == set(receipt["member_sha256"]),
            "Archive members disagree",
        )
        for name, expected in receipt["member_sha256"].items():
            require(
                hashlib.sha256(saved.read(name)).hexdigest() == expected,
                "Archive member digest mismatch",
            )
        manifest = json.loads(saved.read("custom_components/lanbon_mqtt/manifest.json"))
        require(manifest["version"] == tag[1:], "Packaged manifest does not match tag")
    return paths


def verify_assets(tag, release, paths, *, draft):
    require(
        release and release["tagName"] == tag and release["isDraft"] is draft,
        "Release identity/draft state changed",
    )
    names = [asset["name"] for asset in release["assets"]]
    require(
        len(names) == len(set(names)) and set(names) == set(paths),
        "Release assets differ from the verified package",
    )
    if not draft:
        require(release["isPrerelease"] == ("-" in tag), "Published prerelease state differs")
    with tempfile.TemporaryDirectory(prefix="lanbon-release-") as directory:
        command("gh", "release", "download", tag, "--dir", directory)
        actual = Path(directory)
        require(
            {path.name for path in actual.iterdir()} == set(paths), "Downloaded asset list differs"
        )
        for name, source in paths.items():
            require(
                (actual / name).read_bytes() == source.read_bytes(),
                f"Downloaded asset differs: {name}",
            )


def publish(tag, dist):
    paths = package(tag, Path(dist))
    head = command("git", "rev-parse", "HEAD")
    require(
        command("git", "rev-parse", f"refs/tags/{tag}^{{commit}}") == head,
        "Local tag does not match checked-out source",
    )
    repo = os.environ.get("GH_REPO", "")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo), "GH_REPO is required")
    require(
        command("gh", "api", f"repos/{repo}/commits/{tag}", "--jq", ".sha") == head,
        "Remote tag does not match checked-out source",
    )
    release = view(tag)
    if release and not release["isDraft"]:
        verify_assets(tag, release, paths, draft=False)
        print(f"Published {tag} already matches the verified package; no changes made")
        return
    latest = latest_flag(tag, repo)
    if release is None:
        flags = ["--prerelease"] if "-" in tag else []
        command(
            "gh",
            "release",
            "create",
            tag,
            "--verify-tag",
            "--draft",
            "--latest=false",
            "--title",
            f"Lanbon MQTT {tag}",
            "--generate-notes",
            *flags,
        )
    # Re-read immediately before allowing draft-only replacement. Workflow
    # concurrency serializes publication attempts for the same immutable tag.
    release = view(tag)
    require(
        release and release["tagName"] == tag and release["isDraft"],
        "Only an existing draft may receive uploads",
    )
    require(
        {asset["name"] for asset in release["assets"]} <= set(paths),
        "Draft contains unexpected assets; human review required",
    )
    command(
        "gh", "release", "upload", tag, *(str(paths[name]) for name in sorted(paths)), "--clobber"
    )
    verify_assets(tag, view(tag), paths, draft=True)
    command(
        "gh",
        "release",
        "edit",
        tag,
        "--draft=false",
        f"--prerelease={str('-' in tag).lower()}",
        latest,
    )
    verify_assets(tag, view(tag), paths, draft=False)
    print(f"Published verified {tag}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--dist", default="dist")
    args = parser.parse_args()
    publish(args.tag, args.dist)


if __name__ == "__main__":
    main()
