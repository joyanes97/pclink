#!/usr/bin/env python3
"""
PCLink Version File Updater

This is a helper script that takes a version string and updates it in all
relevant project files. It is called by the main release script.
"""

import sys
import argparse
import re
from pathlib import Path

FILES_TO_UPDATE = {
    "src/pclink/core/version.py": r'__version__ = "[^"]+"',
    "pyproject.toml": r'version = "[^"]+"',
}


def get_version_updates(version: str):
    """Return the version-file changes without writing them to disk."""
    updates = {}

    for file_str, pattern in FILES_TO_UPDATE.items():
        path = Path(file_str)
        if not path.exists():
            print(f"  - Warning: {path} not found, skipping.")
            continue

        content = path.read_text(encoding="utf-8")
        # This regex replacement preserves the key ('version' or '__version__')
        new_content = re.sub(
            pattern, f'{pattern.split("=")[0].strip()} = "{version}"', content
        )
        if new_content != content:
            updates[path] = new_content

    return updates


def update_version(version: str):
    """Update version in all necessary project files."""
    print(f"Updating version to '{version}' in all relevant files...")

    updates = get_version_updates(version)
    for path, new_content in updates.items():
        path.write_text(new_content, encoding="utf-8")
        print(f"  - Updated: {path}")

    if not updates:
        print("  - No version changes were necessary.")

    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update version in project files.")
    parser.add_argument("version", help="New version number (e.g., 1.0.1)")
    args = parser.parse_args()

    if not re.match(r"^\d+\.\d+\.\d+(-[\w.-]+)?$", args.version):
        print(f"Error: Invalid version format: {args.version}")
        sys.exit(1)

    if update_version(args.version):
        print("Version update complete.")
    else:
        print("Version update failed.")
        sys.exit(1)
