#!/usr/bin/env python3
"""
PCLink Universal Release Manager

This script consolidates the release process by:
1. Performing safety checks (clean git status, synced branch, gh auth).
2. Prompting for a new version number.
3. Generating release notes from curated notes, GitHub, and the git log.
4. Updating version files and CHANGELOG.md.
5. Committing, tagging, and pushing the new version to GitHub.
6. Creating a GitHub release locally.
7. Clearing RELEASE_NOTES.md only after the release is verified.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from version_updater import get_version_updates  # noqa: E402

DEFAULT_BRANCH = "main"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[\w.-]+)?$")
SEMVER_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
VERSION_HEADING_RE = re.compile(r"\n## \[\d+\.\d+\.\d+[^\]]*\]")
CONVENTIONAL_COMMIT_RE = re.compile(r"^([A-Za-z]+)(?:\([^)]*\))?!?:\s*(.+)$")
HEADING_RE = re.compile(r"^(#{1,6})(\s+.*)$")

COMMIT_GROUPS = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "security": "Security",
    "refactor": "Refactoring",
    "docs": "Other Changes",
    "chore": "Other Changes",
    "style": "Other Changes",
    "test": "Other Changes",
    "build": "Other Changes",
    "ci": "Other Changes",
    "revert": "Other Changes",
}

GROUP_ORDER = [
    "Features",
    "Bug Fixes",
    "Performance",
    "Security",
    "Refactoring",
    "Other Changes",
]

PR_LINK_RE = re.compile(r"/pull/(\d+)")
PR_SUFFIX_RE = re.compile(r"\s*\(#(\d+)\)\s*$")
GENERATED_AUTHOR_RE = re.compile(r"\s+by\s+@([A-Za-z0-9_.-]+)")


class Colors:
    HEADER = "\033[95m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"


class ReleaseError(RuntimeError):
    """A controlled release failure that should be shown without a traceback."""


def print_color(text, color):
    print(f"{color}{text}{Colors.ENDC}")


def run_capture(command, allow_errors=False):
    """Run a command and capture its output without echoing it."""
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as exc:
        raise ReleaseError(f"Could not run '{command[0]}': {exc}") from exc

    if result.returncode != 0 and not allow_errors:
        detail = (result.stderr or result.stdout or "").strip()
        message = f"Command failed: {' '.join(command)}"
        if detail:
            message = f"{message}\n{detail}"
        raise ReleaseError(message)

    return result


def run_command(command, allow_errors=False):
    """Run a command with its output streamed to the terminal."""
    print_color(f"\n> {' '.join(command)}", Colors.OKCYAN)
    try:
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as exc:
        raise ReleaseError(f"Could not run '{command[0]}': {exc}") from exc

    if result.returncode != 0 and not allow_errors:
        raise ReleaseError(f"Command failed: {' '.join(command)}")

    return result


def parse_semver(value):
    """Parse a version into a comparable key, including prerelease ordering."""
    match = SEMVER_RE.match(value.strip())
    if not match:
        return None

    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    prerelease = match.group(4)
    if prerelease is None:
        prerelease_key = (1,)
    else:
        identifiers = []
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                identifiers.append((0, int(identifier)))
            else:
                identifiers.append((1, identifier))
        prerelease_key = (0, tuple(identifiers))

    return (core, prerelease_key)


def bump_version(base_version, bump):
    """Return the next version after applying a semantic-version bump."""
    parsed = parse_semver(base_version)
    if parsed is None:
        major, minor, patch = 0, 0, 0
    else:
        major, minor, patch = parsed[0]

    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        major, minor, patch = major, minor + 1, 0
    else:
        major, minor, patch = major, minor, patch + 1

    return f"{major}.{minor}.{patch}"


def get_latest_tag():
    """Return the highest reachable v-prefixed tag, if one exists."""
    result = run_capture(
        ["git", "tag", "--merged", "HEAD", "--list", "v*", "--sort=-v:refname"],
        allow_errors=True,
    )
    if result.returncode != 0:
        return None

    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for tag in tags:
        if parse_semver(tag):
            return tag
    return tags[0] if tags else None


def release_exists(repo, tag):
    """Return True if a GitHub release already exists for the tag."""
    result = run_capture(
        ["gh", "release", "view", tag, "--repo", repo],
        allow_errors=True,
    )
    if result.returncode == 0:
        return True

    detail = (result.stderr or result.stdout or "").lower()
    if "release not found" in detail or "not found" in detail:
        return False

    raise ReleaseError(
        f"Could not check whether a release exists for {tag}:\n"
        f"{(result.stderr or result.stdout or '').strip()}"
    )


def get_repo_slug():
    """Resolve the GitHub owner/repo from gh, falling back to the remote URL."""
    result = run_capture(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        allow_errors=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    remote_url = run_capture(["git", "remote", "get-url", "origin"]).stdout.strip()
    match = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url)
    if not match:
        raise ReleaseError(f"Could not determine owner/repo from remote: {remote_url}")

    return f"{match.group(1)}/{match.group(2)}"


def preflight(args):
    """Validate all prerequisites before any file is modified."""
    print_color("\n[1/8] Performing preflight checks...", Colors.BOLD)

    repo_root = Path(__file__).resolve().parent.parent
    toplevel = run_capture(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
    if Path(toplevel).resolve() != repo_root:
        raise ReleaseError(f"Expected repository root {repo_root}, found {toplevel}.")

    if not Path("CHANGELOG.md").exists():
        raise ReleaseError("CHANGELOG.md not found in the project root.")

    status = run_capture(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        if args.force:
            print_color(
                "Warning: working directory is not clean (continuing due to --force).",
                Colors.WARNING,
            )
        else:
            raise ReleaseError(
                "Working directory is not clean. Commit or stash your changes, "
                "or rerun with --force."
            )
    else:
        print_color("✓ Git working directory is clean.", Colors.OKGREEN)

    branch = run_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if branch != DEFAULT_BRANCH:
        raise ReleaseError(
            f"Releases must be created from '{DEFAULT_BRANCH}' "
            f"(current branch: '{branch}')."
        )
    print_color(f"✓ On branch {branch}.", Colors.OKGREEN)

    print_color("Fetching tags from origin...", Colors.BOLD)
    run_command(["git", "fetch", "--tags", "origin"])

    head = run_capture(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote_result = run_capture(
        ["git", "rev-parse", f"origin/{DEFAULT_BRANCH}"], allow_errors=True
    )
    if remote_result.returncode != 0:
        raise ReleaseError(f"Could not resolve origin/{DEFAULT_BRANCH}.")
    remote_head = remote_result.stdout.strip()
    if head != remote_head:
        raise ReleaseError(
            f"Local {DEFAULT_BRANCH} ({head[:8]}) is not in sync with "
            f"origin/{DEFAULT_BRANCH} ({remote_head[:8]}).\n"
            "Pull/push first so GitHub can generate notes from the correct commits."
        )
    print_color(
        f"✓ {DEFAULT_BRANCH} is in sync with origin ({head[:8]}).", Colors.OKGREEN
    )

    if shutil.which("gh") is None:
        raise ReleaseError(
            "GitHub CLI (gh) is required. Install it from https://cli.github.com/."
        )

    auth = run_capture(["gh", "auth", "status"], allow_errors=True)
    if auth.returncode != 0:
        detail = (auth.stderr or auth.stdout or "").strip()
        raise ReleaseError(
            "GitHub CLI is not authenticated. Run 'gh auth login' first."
            + (f"\n{detail}" if detail else "")
        )
    print_color("✓ GitHub CLI is installed and authenticated.", Colors.OKGREEN)

    repo = get_repo_slug()
    print_color(f"✓ Target repository: {repo}.", Colors.OKGREEN)

    if not Path("RELEASE_NOTES.md").exists():
        print_color(
            "Warning: RELEASE_NOTES.md not found; generated/git notes will be used.",
            Colors.WARNING,
        )

    return {
        "repo": repo,
        "head": head,
        "latest_tag": get_latest_tag(),
    }


def resolve_version(args, latest_tag):
    """Resolve and validate the version to release."""
    print_color("\n[2/8] Determining version number...", Colors.BOLD)

    if args.version and args.bump:
        raise ReleaseError("Use either --version or --bump, not both.")

    latest_version = latest_tag[1:] if latest_tag else None

    if args.bump:
        base = latest_version or "0.0.0"
        version = bump_version(base, args.bump)
    elif args.version:
        version = args.version.strip()
        if not VERSION_RE.match(version):
            raise ReleaseError(
                f"Invalid version '{version}'. Use Semantic Versioning "
                "(X.Y.Z or X.Y.Z-beta.1)."
            )
    else:
        while True:
            new_version = input(
                "Enter new version (e.g., 1.0.0 or 1.0.0-beta.1): "
            ).strip()
            if VERSION_RE.match(new_version):
                version = new_version
                break
            print_color(
                "Invalid format. Use Semantic Versioning (X.Y.Z or X.Y.Z-beta.1).",
                Colors.FAIL,
            )

    is_beta = args.beta or bool(
        re.search(r"-(?:beta|alpha|rc)(?:[.\-]|$)", version, re.IGNORECASE)
    )

    if latest_version:
        new_key = parse_semver(version)
        old_key = parse_semver(latest_version)
        if new_key and old_key and new_key <= old_key:
            raise ReleaseError(
                f"Version {version} must be greater than the latest tag {latest_tag}."
            )

    tag = f"v{version}"
    release_type = "BETA" if is_beta else "STABLE"
    print_color(
        f"✓ Version set to {version} (tag: {tag}, type: {release_type})",
        Colors.OKGREEN,
    )
    if latest_tag:
        print_color(f"  Previous release: {latest_tag}", Colors.OKCYAN)

    return version, is_beta, tag


def validate_target(repo, tag):
    """Ensure the target tag and GitHub release do not already exist."""
    local_tag = run_capture(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        allow_errors=True,
    )
    if local_tag.returncode == 0:
        raise ReleaseError(f"Local tag {tag} already exists.")

    remote_tag = run_capture(
        ["git", "ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{tag}"],
        allow_errors=True,
    )
    if remote_tag.returncode == 0:
        raise ReleaseError(f"Remote tag {tag} already exists on origin.")

    if release_exists(repo, tag):
        raise ReleaseError(f"A GitHub release for {tag} already exists.")

    print_color(f"✓ Tag {tag} is available locally and remotely.", Colors.OKGREEN)


def generate_github_notes(repo, tag, previous_tag, head_sha):
    """Ask GitHub's Release Notes API for generated notes before tagging."""
    print_color("Requesting GitHub-generated release notes...", Colors.BOLD)

    command = [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/releases/generate-notes",
        "-f",
        f"tag_name={tag}",
        "-f",
        f"target_commitish={head_sha}",
    ]
    if previous_tag:
        command.extend(["-f", f"previous_tag_name={previous_tag}"])

    result = run_capture(command, allow_errors=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print_color(
            "Warning: GitHub note generation failed; continuing with the "
            "curated/git-log notes." + (f"\n{detail}" if detail else ""),
            Colors.WARNING,
        )
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print_color(
            "Warning: GitHub returned invalid note data; continuing with fallback.",
            Colors.WARNING,
        )
        return None

    body = (payload.get("body") or "").strip()
    if not body:
        print_color(
            "Warning: GitHub returned empty notes; continuing with fallback.",
            Colors.WARNING,
        )
        return None

    print_color("✓ Received GitHub-generated release notes.", Colors.OKGREEN)
    return body


def split_conventional_subject(subject):
    """Return (type, cleaned message, PR number) for a commit subject."""
    subject = subject.strip()
    match = CONVENTIONAL_COMMIT_RE.match(subject)
    if match:
        commit_type = match.group(1).lower()
        message = match.group(2).strip()
    else:
        commit_type = "other"
        message = subject

    pr_number = None
    pr_match = PR_SUFFIX_RE.search(message)
    if pr_match:
        pr_number = pr_match.group(1)
        message = message[: pr_match.start()].strip()

    return commit_type, re.sub(r"\s+", " ", message).strip(), pr_number


def make_note_entry(subject, author, short_hash="", pr_number=None):
    """Create a normalized note entry from a commit subject."""
    commit_type, message, subject_pr = split_conventional_subject(subject)
    if not message:
        message = subject.strip()

    return {
        "type": commit_type,
        "group": COMMIT_GROUPS.get(commit_type, "Other Changes"),
        "message": message,
        "author": author.strip(),
        "short_hash": short_hash.strip(),
        "pr": pr_number or subject_pr,
    }


def git_log_entries(previous_tag):
    """Parse commits since the previous tag into structured note entries."""
    revision_range = f"{previous_tag}..HEAD" if previous_tag else "HEAD"
    result = run_capture(
        [
            "git",
            "log",
            "--no-merges",
            "--format=%h%x09%an%x09%s",
            revision_range,
        ]
    )

    entries = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        short_sha, author, subject = (part.strip() for part in parts)
        if not subject:
            continue
        if subject.startswith("Bump version to "):
            continue
        if subject.lower().startswith("chore: reset release_notes"):
            continue

        entries.append(make_note_entry(subject, author, short_hash=short_sha))

    return entries


def format_author(author):
    """Format a Git author name as a GitHub-style mention."""
    value = author.strip()
    if value.startswith("@"):
        value = value[1:]
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^\w.@-]", "", value)
    return f"@{value}" if value else "@unknown"


def format_note_entry(entry):
    """Render one bullet in the style: message by @author in reference."""
    author = format_author(entry.get("author", ""))
    if entry.get("pr"):
        reference = f"#{entry['pr']}"
    else:
        reference = entry.get("short_hash", "")

    if reference:
        return f"* {entry['message']} by {author} in {reference}"
    return f"* {entry['message']} by {author}"


def note_entry_key(entry):
    """Return a stable key used to deduplicate git and GitHub entries."""
    if entry.get("pr"):
        return f"pr:{entry['pr']}"
    message = re.sub(r"\W+", " ", entry.get("message", "").lower()).strip()
    return f"message:{message}"


def note_message_key(entry):
    return re.sub(r"\W+", " ", entry.get("message", "").lower()).strip()


def merge_note_entries(git_entries, generated_entries):
    """Merge GitHub PR entries into git entries, preferring GitHub usernames."""
    generated_by_pr = {
        entry["pr"]: entry for entry in generated_entries if entry.get("pr")
    }
    generated_by_message = {
        note_message_key(entry): entry
        for entry in generated_entries
        if note_message_key(entry)
    }

    used_generated = set()
    merged = []
    for git_entry in git_entries:
        match = None
        if git_entry.get("pr"):
            match = generated_by_pr.get(git_entry["pr"])
        if match is None:
            match = generated_by_message.get(note_message_key(git_entry))

        if match is not None:
            used_generated.add(id(match))
            git_entry = dict(git_entry)
            git_entry["pr"] = git_entry.get("pr") or match.get("pr")
            if match.get("author"):
                git_entry["author"] = match["author"]
            if git_entry.get("type") == "other" and match.get("type") != "other":
                git_entry["type"] = match["type"]
                git_entry["group"] = match["group"]

        merged.append(git_entry)

    for generated_entry in generated_entries:
        if id(generated_entry) in used_generated:
            continue
        merged.append(generated_entry)

    unique = []
    seen = set()
    for entry in merged:
        key = note_entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    return unique


def format_release_note_entries(entries):
    """Format structured entries into KDE-style categorized markdown."""
    grouped = {}
    for entry in entries:
        group = entry.get("group") or "Other Changes"
        bullet = format_note_entry(entry)
        grouped.setdefault(group, [])
        if bullet not in grouped[group]:
            grouped[group].append(bullet)

    sections = []
    for group in GROUP_ORDER:
        bullets = grouped.get(group)
        if bullets:
            sections.append(f"### {group}\n\n" + "\n".join(bullets))

    return "\n\n".join(sections)


def normalize_heading_levels(markdown, minimum_level=3):
    """Demote note headings so they sit below the changelog version heading."""
    lines = markdown.splitlines()

    levels = []
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            levels.append(len(match.group(1)))

    if not levels:
        return markdown.strip()

    delta = max(0, minimum_level - min(levels))
    normalized = []
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            normalized.append(line)
            continue
        if in_fence:
            normalized.append(line)
            continue

        match = HEADING_RE.match(line)
        if match:
            level = min(6, len(match.group(1)) + delta)
            normalized.append("#" * level + match.group(2))
        else:
            normalized.append(line)

    return "\n".join(normalized).strip()


def extract_generated_sections(body):
    """Separate generated change sections from the Full Changelog line."""
    if not body:
        return "", None

    compare_line = None
    core_lines = []
    for line in body.splitlines():
        if "full changelog" in line.lower():
            if compare_line is None:
                compare_line = line.strip()
            continue
        core_lines.append(line)

    return "\n".join(core_lines).strip(), compare_line


def parse_github_notes(body):
    """Parse GitHub's generated notes into PR-aware structured entries."""
    if not body:
        return [], None

    core, compare_line = extract_generated_sections(body)
    entries = []
    for line in core.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## new contributors"):
            break
        if not stripped.startswith("* "):
            continue

        content = stripped[2:].strip()
        if not content or "made their first contribution" in content.lower():
            continue

        pr_match = PR_LINK_RE.search(content)
        pr_number = pr_match.group(1) if pr_match else None
        if pr_number is None:
            hash_match = re.search(r"(?:^|\s)#(\d+)\b", content)
            if hash_match:
                pr_number = hash_match.group(1)

        author_match = GENERATED_AUTHOR_RE.search(content)
        author = author_match.group(1) if author_match else ""

        message = re.split(r"\s+by\s+@[A-Za-z0-9_.-]+", content, maxsplit=1)[0]
        message = re.sub(r"\s+in\s+https?://\S+", "", message).strip()
        if not message:
            continue

        entries.append(make_note_entry(message, author, pr_number=pr_number))

    return entries, compare_line


def compose_release_notes(curated, github_body, git_entries, repo, previous_tag, tag):
    """Combine curated notes, GitHub PR entries, and the git-log fallback."""
    generated_entries, compare_line = parse_github_notes(github_body)
    entries = merge_note_entries(git_entries, generated_entries)
    sections = []
    used_git_fallback = not generated_entries and bool(git_entries)

    if curated.strip():
        sections.append(normalize_heading_levels(curated))

    formatted_entries = format_release_note_entries(entries)
    if formatted_entries:
        sections.append(formatted_entries)
    elif github_body:
        generated_core, _ = extract_generated_sections(github_body)
        if generated_core:
            sections.append(normalize_heading_levels(generated_core))

    if not sections:
        sections.append("- No changes documented")

    has_compare_line = any("**Full Changelog**" in section for section in sections)
    if not compare_line and previous_tag and not has_compare_line:
        compare_line = (
            f"**Full Changelog**: https://github.com/{repo}/compare/"
            f"{previous_tag}...{tag}"
        )

    if compare_line and not has_compare_line:
        sections.append(compare_line)

    notes = "\n\n".join(section.strip() for section in sections if section.strip())
    return notes.strip(), used_git_fallback


def build_changelog_content(version, notes, allow_existing=False):
    """Build the new changelog content in memory."""
    changelog_path = Path("CHANGELOG.md")
    content = changelog_path.read_text(encoding="utf-8")

    if f"## [{version}]" in content:
        if allow_existing:
            print_color(
                f"Warning: Version {version} already exists in CHANGELOG.md; "
                "skipping the changelog update.",
                Colors.WARNING,
            )
            return None
        raise ReleaseError(
            f"Version {version} already exists in CHANGELOG.md. "
            "Bump the version or pass --allow-existing-changelog."
        )

    today = datetime.now().strftime("%Y-%m-%d")
    version_heading = f"## [{version}] - {today}"
    body = notes.strip() or "- No changes documented"

    match = VERSION_HEADING_RE.search(content)
    if match:
        header = content[: match.start()].rstrip()
        rest = content[match.start() :].lstrip("\n").rstrip()
        return f"{header}\n\n{version_heading}\n\n{body}\n\n{rest}\n"

    return f"{content.rstrip()}\n\n{version_heading}\n\n{body}\n"


def print_diff(path, old_content, new_content):
    """Print a unified diff for a proposed file change."""
    print_color(f"\n--- Proposed changes to {path} ---", Colors.BOLD)
    diff = list(
        difflib.unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if not diff:
        print("(no changes)")
        return
    for line in diff:
        print(line)


def print_notes_preview(notes):
    print_color("\n--- Release notes preview ---", Colors.BOLD)
    print(notes)
    print_color("--- End release notes ---", Colors.BOLD)


def apply_file_updates(updates):
    """Write precomputed version-file updates."""
    for path, content in updates.items():
        path.write_text(content, encoding="utf-8")
        print_color(f"✓ Updated {path}.", Colors.OKGREEN)


def create_github_release(repo, tag, version, notes, is_beta):
    """Create the GitHub release with the exact notes written to the changelog."""
    title = f"PCLink v{version}"
    if is_beta:
        title += " (Beta)"

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".md", delete=False
        ) as handle:
            handle.write(notes)
            temp_path = Path(handle.name)

        command = [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repo,
            "--verify-tag",
            "--title",
            title,
            "--notes-file",
            str(temp_path),
        ]
        if is_beta:
            command.extend(["--prerelease", "--latest=false"])
        else:
            command.append("--latest")

        result = run_command(command, allow_errors=True)
        if result.returncode != 0:
            if release_exists(repo, tag):
                raise ReleaseError(
                    f"Release {tag} already exists; refusing to overwrite its notes."
                )
            raise ReleaseError(f"Failed to create GitHub release {tag}.")

        view = run_capture(
            [
                "gh",
                "release",
                "view",
                tag,
                "--repo",
                repo,
                "--json",
                "url,isPrerelease,tagName",
            ]
        )
        try:
            payload = json.loads(view.stdout)
            url = payload.get("url", "")
        except json.JSONDecodeError:
            url = ""

        print_color(f"✓ Created GitHub release: {title}", Colors.OKGREEN)
        if url:
            print_color(f"  {url}", Colors.OKCYAN)
        if is_beta:
            print_color("  Marked as pre-release (beta)", Colors.OKCYAN)
        else:
            print_color("  Marked as latest stable release", Colors.OKCYAN)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def finalize_release_notes(version):
    """Clear RELEASE_NOTES.md only after a verified release."""
    print_color("\n[8/8] Finalizing release notes...", Colors.BOLD)
    notes_path = Path("RELEASE_NOTES.md")
    if not notes_path.exists():
        return

    if not notes_path.read_text(encoding="utf-8").strip():
        print_color("✓ RELEASE_NOTES.md is already empty.", Colors.OKGREEN)
        return

    notes_path.write_text("", encoding="utf-8")
    print_color("✓ Cleared RELEASE_NOTES.md for the next release.", Colors.OKGREEN)

    tracked = run_capture(
        ["git", "ls-files", "--error-unmatch", "--", str(notes_path)],
        allow_errors=True,
    )
    if tracked.returncode != 0:
        print_color(
            "✓ RELEASE_NOTES.md is not tracked by Git; no cleanup commit needed.",
            Colors.OKGREEN,
        )
        return

    run_command(["git", "add", "--", str(notes_path)])
    commit = run_command(
        [
            "git",
            "commit",
            "-m",
            f"chore: reset release notes after v{version}",
            "--",
            str(notes_path),
        ],
        allow_errors=True,
    )
    if commit.returncode != 0:
        print_color(
            "Warning: Could not commit the RELEASE_NOTES.md reset; commit it manually.",
            Colors.WARNING,
        )
        return

    push = run_command(["git", "push", "origin", "HEAD"], allow_errors=True)
    if push.returncode != 0:
        print_color(
            "Warning: Release succeeded, but the RELEASE_NOTES.md cleanup "
            "commit was not pushed.",
            Colors.WARNING,
        )
    else:
        print_color("✓ Pushed RELEASE_NOTES.md cleanup commit.", Colors.OKGREEN)


def print_dry_run(
    version,
    tag,
    is_beta,
    previous_tag,
    repo,
    notes,
    version_updates,
    changelog_old,
    changelog_new,
    changelog_changed,
    staged_paths,
):
    """Print the complete plan without modifying anything."""
    print_color("\n--- DRY RUN: no changes will be made ---", Colors.HEADER)
    print(f"Version:       {version}")
    print(f"Tag:           {tag}")
    print(f"Release type:  {'BETA' if is_beta else 'STABLE'}")
    print(f"Previous tag:  {previous_tag or '(none)'}")
    print(f"Repository:    {repo}")
    print(f"Commit:        Bump version to {version}")
    print(f"Stage:         {', '.join(staged_paths)}")

    print_notes_preview(notes)

    print_color("\n--- Version file changes ---", Colors.BOLD)
    for path, content in version_updates.items():
        print_diff(path, path.read_text(encoding="utf-8"), content)

    if changelog_changed:
        print_diff("CHANGELOG.md", changelog_old, changelog_new)
    else:
        print_color("\nCHANGELOG.md already contains this version.", Colors.WARNING)

    print_color("\n--- Commands that would run ---", Colors.BOLD)
    print(f"git add -- {' '.join(staged_paths)}")
    print(f"git commit -m 'Bump version to {version}' -- {' '.join(staged_paths)}")
    print(f"git tag -a {tag} -m 'Release {version}'")
    print(f"git push --atomic origin HEAD refs/tags/{tag}")
    release_flags = "--prerelease --latest=false" if is_beta else "--latest"
    print(
        f"gh release create {tag} --repo {repo} --verify-tag "
        f"--title 'PCLink v{version}' --notes-file <generated> {release_flags}"
    )
    print(
        "RELEASE_NOTES.md will be cleared only after the GitHub release is "
        "verified (committed only if the file is tracked)."
    )
    print_color(
        "\nDry run complete. No files, commits, tags, or releases changed.",
        Colors.OKGREEN,
    )


def run_release(args):
    print_color("--- PCLink Universal Release Manager ---", Colors.HEADER)

    context = preflight(args)
    repo = context["repo"]
    previous_tag = context["latest_tag"]

    version, is_beta, tag = resolve_version(args, previous_tag)
    validate_target(repo, tag)

    print_color("\n[3/8] Generating release notes...", Colors.BOLD)
    curated = ""
    release_notes_path = Path("RELEASE_NOTES.md")
    if release_notes_path.exists():
        curated = release_notes_path.read_text(encoding="utf-8").strip()
        if curated:
            print_color("✓ Using curated RELEASE_NOTES.md content.", Colors.OKGREEN)
        else:
            print_color(
                "RELEASE_NOTES.md is empty; generated notes will be used.",
                Colors.WARNING,
            )

    github_notes = generate_github_notes(repo, tag, previous_tag, context["head"])
    git_entries = git_log_entries(previous_tag)
    notes, used_git_fallback = compose_release_notes(
        curated, github_notes, git_entries, repo, previous_tag, tag
    )

    if used_git_fallback:
        print_color("✓ Added git-log fallback notes.", Colors.OKGREEN)

    print_color("\n[4/8] Preparing version and changelog updates...", Colors.BOLD)
    version_updates = get_version_updates(version)
    if not version_updates:
        raise ReleaseError(
            "No version files would change. Check the version and "
            "scripts/version_updater.py."
        )

    changelog_old = Path("CHANGELOG.md").read_text(encoding="utf-8")
    changelog_new = build_changelog_content(
        version, notes, allow_existing=args.allow_existing_changelog
    )
    changelog_changed = changelog_new is not None and changelog_new != changelog_old
    if changelog_new is None:
        changelog_new = changelog_old

    staged_paths = [str(path) for path in version_updates]
    if changelog_changed:
        staged_paths.append("CHANGELOG.md")

    print_color("\n[5/8] Review and confirm...", Colors.BOLD)
    print("The script will now perform the following actions:")
    print(f"  1. Stage: {', '.join(staged_paths)}")
    print(f"  2. Commit with message: Bump version to {version}")
    print(f"  3. Create Git tag: {tag}")
    print("  4. Push commit and tag atomically to 'origin'.")
    print(
        "  5. Create GitHub release "
        f"({'pre-release/beta' if is_beta else 'latest stable'})."
    )
    print(
        "  6. Clear RELEASE_NOTES.md after the release is verified "
        "(commit only if tracked)."
    )

    if args.dry_run:
        print_dry_run(
            version,
            tag,
            is_beta,
            previous_tag,
            repo,
            notes,
            version_updates,
            changelog_old,
            changelog_new,
            changelog_changed,
            staged_paths,
        )
        return

    print_notes_preview(notes)
    for path, content in version_updates.items():
        print_diff(path, path.read_text(encoding="utf-8"), content)
    if changelog_changed:
        print_diff("CHANGELOG.md", changelog_old, changelog_new)
    else:
        print_color("\nCHANGELOG.md already contains this version.", Colors.WARNING)

    if not args.force:
        try:
            answer = input(f"\n{Colors.WARNING}Proceed? (y/n): {Colors.ENDC}")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            raise ReleaseError("Release cancelled.")

    print_color(
        "\n[6/8] Applying changes, committing, tagging, and pushing...", Colors.BOLD
    )
    apply_file_updates(version_updates)
    if changelog_changed:
        Path("CHANGELOG.md").write_text(changelog_new, encoding="utf-8")
        print_color("✓ Updated CHANGELOG.md.", Colors.OKGREEN)

    run_command(["git", "add", "--"] + staged_paths)
    staged_diff = run_capture(
        ["git", "diff", "--cached", "--quiet", "--"] + staged_paths,
        allow_errors=True,
    )
    if staged_diff.returncode == 0:
        raise ReleaseError("No release changes were staged; aborting.")

    run_command(["git", "diff", "--cached", "--stat", "--"] + staged_paths)
    run_command(
        ["git", "commit", "-m", f"Bump version to {version}", "--"] + staged_paths
    )

    commit_sha = run_capture(["git", "rev-parse", "HEAD"]).stdout.strip()
    run_command(["git", "tag", "-a", tag, "-m", f"Release {version}"])

    local_tag = run_capture(["git", "rev-parse", f"refs/tags/{tag}"]).stdout.strip()
    tag_commit = run_capture(["git", "rev-parse", f"{tag}^{{commit}}"]).stdout.strip()
    if tag_commit != commit_sha:
        raise ReleaseError(f"Tag {tag} does not point at the release commit.")

    try:
        run_command(["git", "push", "--atomic", "origin", "HEAD", f"refs/tags/{tag}"])
    except ReleaseError as exc:
        raise ReleaseError(
            f"{exc}\nThe release commit and tag exist locally but were not "
            "pushed. Inspect them with 'git log --oneline -3' and "
            f"'git show {tag}' before retrying."
        ) from exc

    remote_tag = run_capture(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"]
    ).stdout.strip()
    if not remote_tag:
        raise ReleaseError(f"Tag {tag} was not found on origin after the push.")
    if remote_tag.split()[0] != local_tag:
        raise ReleaseError(f"Remote tag {tag} does not match the local tag.")

    remote_branch = run_capture(
        ["git", "ls-remote", "origin", f"refs/heads/{DEFAULT_BRANCH}"]
    ).stdout.strip()
    if not remote_branch or remote_branch.split()[0] != commit_sha:
        raise ReleaseError(
            f"origin/{DEFAULT_BRANCH} does not point at the release commit."
        )
    print_color("✓ Commit and tag pushed atomically to origin.", Colors.OKGREEN)

    print_color("\n[7/8] Creating GitHub release...", Colors.BOLD)
    create_github_release(repo, tag, version, notes, is_beta)

    finalize_release_notes(version)

    print_color("\n--- Release Process Complete! ---", Colors.HEADER)
    print_color("✓ Successfully pushed commit and tag to GitHub.", Colors.OKGREEN)
    print_color(
        f"✓ Created GitHub release {tag} from the local script.", Colors.OKGREEN
    )
    print(
        "The GitHub Actions workflow will now build and upload the official "
        "release artifacts without changing the release notes."
    )


def main():
    parser = argparse.ArgumentParser(description="PCLink Release Manager")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow a dirty tree and skip the confirmation prompt.",
    )
    parser.add_argument(
        "--version",
        help="Specify version number directly (e.g., 1.2.0 or 1.2.0-beta.1).",
    )
    parser.add_argument(
        "--bump",
        choices=["major", "minor", "patch"],
        help="Compute the next version from the latest Git tag.",
    )
    parser.add_argument(
        "--beta", action="store_true", help="Mark this as a beta/pre-release."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned notes and changes without modifying anything.",
    )
    parser.add_argument(
        "--allow-existing-changelog",
        action="store_true",
        help="Skip the changelog update if the version already exists.",
    )
    args = parser.parse_args()

    os.chdir(Path(__file__).resolve().parent.parent)

    try:
        run_release(args)
    except ReleaseError as exc:
        print_color(f"\nError: {exc}", Colors.FAIL)
        sys.exit(1)
    except KeyboardInterrupt:
        print_color("\nRelease cancelled.", Colors.WARNING)
        sys.exit(130)


if __name__ == "__main__":
    main()
