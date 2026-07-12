#!/usr/bin/env python3
"""Insert/update a single version entry in the local packages.json ledger.

This file (packaging/packages.json) is the git-tracked source of truth for
KiCad Plugin and Content Manager version history. It is kept up to date in
two places:

  1. The .githooks/pre-push hook calls this script (with no download_*
     fields available yet) to add a placeholder entry for the version it is
     about to tag, so the ledger always has a row for every released tag.
  2. The release.yml CI workflow calls this script again once the release
     zip has actually been built and uploaded, this time passing the real
     download_sha256/download_url/download_size/install_size, and commits
     the result back to the repository.

Using a single git-tracked ledger (instead of re-downloading the previous
packages.json from the GitHub "latest" release alias on every build) avoids
losing version history to network flakiness or release-ordering races.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = PACKAGING_DIR / "metadata.template.json"
LEDGER_PATH = PACKAGING_DIR / "packages.json"


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Version number, e.g. 0.1.14")
    parser.add_argument("--download-sha256")
    parser.add_argument("--download-url")
    parser.add_argument("--download-size", type=int)
    parser.add_argument("--install-size", type=int)
    args = parser.parse_args()

    template = load_json(TEMPLATE_PATH)
    identifier = template["identifier"]

    if LEDGER_PATH.exists():
        ledger = load_json(LEDGER_PATH)
    else:
        ledger = {"packages": []}

    packages = ledger.setdefault("packages", [])
    entry = next((p for p in packages if p.get("identifier") == identifier), None)
    if entry is None:
        entry = {k: v for k, v in template.items() if k != "versions"}
        entry["versions"] = []
        packages.append(entry)
    else:
        for key, value in template.items():
            if key != "versions":
                entry[key] = value

    version_entry = dict(template["versions"][0])
    version_entry["version"] = args.version

    if args.download_sha256 is not None:
        version_entry["download_sha256"] = args.download_sha256
    if args.download_url is not None:
        version_entry["download_url"] = args.download_url
    if args.download_size is not None:
        version_entry["download_size"] = args.download_size
    if args.install_size is not None:
        version_entry["install_size"] = args.install_size

    existing_versions = [v for v in entry["versions"] if v.get("version") != args.version]
    prior = next((v for v in entry["versions"] if v.get("version") == args.version), None)
    if prior is not None:
        # Preserve already-known download_* fields if this call doesn't
        # supply new ones (e.g. the pre-push placeholder call running again).
        for key in ("download_sha256", "download_url", "download_size", "install_size"):
            version_entry.setdefault(key, prior.get(key))

    existing_versions.insert(0, version_entry)
    entry["versions"] = existing_versions

    write_json(LEDGER_PATH, ledger)
    print(f"packaging/packages.json updated for version {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
