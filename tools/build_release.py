"""Build a reproducible manual-install ZIP without including development or private data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = Path("custom_components/lanbon_mqtt")
RUNTIME_FILES = {
    "__init__.py",
    "button.py",
    "config_flow.py",
    "const.py",
    "diagnostics.py",
    "discovery.py",
    "hub.py",
    "light.py",
    "manifest.json",
    "metadata.py",
    "network.py",
    "observation.py",
    "policy.py",
    "protocol.py",
    "sensor.py",
    "strings.json",
    "switch.py",
    "translations/en.json",
    "brand/icon.png",
    "brand/icon@2x.png",
    "brand/icon.svg",
    "brand/README.md",
}


def build_release(expected_tag: str | None = None) -> dict:
    """Validate the allowlist and write byte-stable ZIP, receipt and checksum files."""
    component = ROOT / COMPONENT
    manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?", version
    ):
        raise ValueError("Manifest version must be a SemVer release or prerelease")
    if expected_tag is not None and expected_tag != f"v{version}":
        raise ValueError(f"Release tag {expected_tag!r} must match manifest v{version}")
    if manifest["domain"] != COMPONENT.name:
        raise ValueError("Manifest domain must match the component directory")
    actual = {
        path.relative_to(component).as_posix()
        for path in component.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if actual != RUNTIME_FILES:
        raise ValueError(
            f"Runtime allowlist mismatch: missing={sorted(RUNTIME_FILES - actual)}, "
            f"unexpected={sorted(actual - RUNTIME_FILES)}"
        )
    files = [ROOT / "LICENSE", *(component / name for name in sorted(RUNTIME_FILES))]
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Release source must be a regular file: {path.relative_to(ROOT)}")
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    archive = destination / f"lanbon-mqtt-{version}.zip"
    hashes = {}
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(files):
            name = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            if path.suffix == ".json":
                json.loads(data)
            # Use fixed timestamps, Unix permissions and order on every build host.
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, data, compresslevel=9)
            hashes[name] = hashlib.sha256(data).hexdigest()
    with ZipFile(archive) as saved:
        if saved.testzip() is not None or set(saved.namelist()) != set(hashes):
            raise ValueError("Release archive integrity failure")
        for name, digest in hashes.items():
            if hashlib.sha256(saved.read(name)).hexdigest() != digest:
                raise ValueError("Release member digest mismatch")
    receipt = {
        "version": version,
        "archive": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "bytes": archive.stat().st_size,
        "file_count": len(hashes),
        "member_sha256": hashes,
    }
    (destination / "release-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (destination / "SHA256SUMS").write_text(
        f"{receipt['sha256']}  {archive.name}\n", encoding="ascii", newline="\n"
    )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Require this exact Git tag, for example v0.3.0-rc.4")
    args = parser.parse_args()
    receipt = build_release(args.tag)
    print(json.dumps({key: value for key, value in receipt.items() if key != "member_sha256"}))


if __name__ == "__main__":
    main()
