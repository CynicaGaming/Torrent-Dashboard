#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "release_notes" / "releases.json"
ACTIVE_WORK = ROOT / "development" / "current.json"


def version_key(value: str):
    raw = str(value or "").strip().lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", raw):
        raise RuntimeError(f"Invalid semantic version in release metadata: {value}")
    main, sep, suffix = raw.partition("-")
    nums = tuple(int(part) for part in main.split("."))
    return (*nums, 1 if not sep else 0, suffix.lower())


def clean_string_list(value, label: str):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise RuntimeError(f"{label} must be a list of non-empty strings")
    return [x.strip() for x in value]


def load_source():
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("releases"), list):
        raise RuntimeError("release_notes/releases.json must contain a releases array")
    seen = set()
    releases = []
    for raw in data["releases"]:
        if not isinstance(raw, dict):
            raise RuntimeError("Every release entry must be an object")
        version = str(raw.get("version") or "").strip().lstrip("vV")
        version_key(version)
        if version in seen:
            raise RuntimeError(f"Duplicate release metadata for v{version}")
        seen.add(version)
        summary = str(raw.get("summary") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not summary or not title:
            raise RuntimeError(f"v{version} requires title and summary")
        item = dict(raw)
        item["version"] = version
        for field in ("highlights", "fixes", "technical", "validation", "known_issues", "architecture", "decisions"):
            item[field] = clean_string_list(item.get(field, []), f"v{version} field {field}")
        next_steps = item.get("next_steps", []) or []
        if not isinstance(next_steps, list):
            raise RuntimeError(f"v{version} next_steps must be a list")
        normalized_steps = []
        for step in next_steps:
            if not isinstance(step, dict):
                raise RuntimeError(f"v{version} next_steps entries must be objects")
            priority = int(step.get("priority") or 0)
            title_text = str(step.get("title") or "").strip()
            detail = str(step.get("detail") or "").strip()
            if priority < 1 or not title_text or not detail:
                raise RuntimeError(f"v{version} next_steps entries require priority, title, and detail")
            normalized_steps.append({"priority": priority, "title": title_text, "detail": detail})
        item["next_steps"] = sorted(normalized_steps, key=lambda x: x["priority"])
        releases.append(item)
    releases.sort(key=lambda x: version_key(x["version"]), reverse=True)
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    return project, releases


def load_active_work():
    if not ACTIVE_WORK.is_file():
        raise RuntimeError("development/current.json is required for development continuity")
    data = json.loads(ACTIVE_WORK.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or int(data.get("schema") or 0) != 1:
        raise RuntimeError("development/current.json must use schema 1")
    for field in ("status", "objective", "why", "next_action"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise RuntimeError(f"development/current.json requires non-empty {field}")
        data[field] = data[field].strip()
    for field in ("acceptance_criteria", "decisions", "files", "blockers", "out_of_scope"):
        data[field] = clean_string_list(data.get(field, []), f"development/current.json field {field}")
    return data


def canonical_repository(project):
    return str(project.get("canonical_repository") or project.get("repository") or "CynicaGaming/Torrent-Dashboard").strip()


def upstream_value(project, new_key, old_key):
    return str(project.get(new_key) or project.get(old_key) or "").strip()


def bullets(items):
    return "\n".join(f"- {item}" for item in items)


def render_release_body(item):
    lines = [f"## v{item['version']} — {item['title']}", "", item["summary"].strip()]
    sections = [
        ("What's changed", item["highlights"]),
        ("Fixes", item["fixes"]),
        ("Technical notes", item["technical"]),
        ("Validation", item["validation"]),
        ("Known issues", item["known_issues"]),
    ]
    for heading, values in sections:
        if values:
            lines.extend(["", f"### {heading}", "", bullets(values)])
    lines.append("")
    return "\n".join(lines)


def render_changelog(releases):
    lines = [
        "# Torrent Dashboard Changelog",
        "",
        "> Generated from `release_notes/releases.json`. Do not edit this file manually.",
        "",
    ]
    for item in releases:
        lines.extend([render_release_body(item).rstrip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def render_project_state(project, releases):
    latest = releases[0]
    canonical = canonical_repository(project)
    dev_branch = upstream_value(project, "upstream_development_branch", "development_branch")
    prerelease_branch = upstream_value(project, "upstream_prerelease_branch", "prerelease_branch")
    upstream_pr = project.get("upstream_pull_request", project.get("pull_request"))
    lines = [
        "# Torrent Dashboard Project State",
        "",
        "> Generated from `release_notes/releases.json`. Do not edit this file manually.",
        "",
        "> **Fork note:** repository and branch references below describe the canonical upstream development line. Forks should verify their own origin, branch, and pull-request state before continuing work.",
        "",
        "## Current baseline",
        "",
        f"- Latest documented build: **v{latest['version']}** ({latest.get('status', 'unknown')})",
        f"- Canonical upstream: `{canonical}`",
    ]
    if dev_branch:
        lines.append(f"- Upstream development branch: `{dev_branch}`")
    if prerelease_branch:
        lines.append(f"- Upstream prerelease branch: `{prerelease_branch}`")
    if upstream_pr:
        lines.append(f"- Upstream active refactor PR: **#{upstream_pr}**")
    lines.extend(["", "### Latest release summary", "", latest["summary"]])

    if latest.get("architecture"):
        lines.extend(["", "## Architecture state", "", bullets(latest["architecture"])])
    if latest.get("decisions"):
        lines.extend(["", "## Current engineering decisions", "", bullets(latest["decisions"])])
    if project.get("principles"):
        lines.extend(["", "## Development principles", "", bullets([str(x).strip() for x in project["principles"] if str(x).strip()])])

    lines.extend(["", "## Recent work", ""])
    for item in releases[:5]:
        lines.extend([f"### v{item['version']} — {item['title']}", "", item["summary"], ""])
        if item["highlights"]:
            lines.extend([bullets(item["highlights"]), ""])

    lines.extend(["## What to do next", ""])
    if latest.get("next_steps"):
        for step in latest["next_steps"]:
            lines.append(f"{step['priority']}. **{step['title']}** — {step['detail']}")
    else:
        lines.append("No next steps are recorded in the latest release metadata.")

    if latest.get("known_issues"):
        lines.extend(["", "## Known issues", "", bullets(latest["known_issues"])])

    lines.extend([
        "",
        "## Handoff instructions for a new development session",
        "",
        "1. Read `HANDOFF.md` first for the combined released-state and active-work view.",
        "2. Verify the repository origin and current branch; do not assume the canonical upstream branch names exist in a fork.",
        "3. Review `development/current.json` for active engineering intent and `release_notes/releases.json` for released history.",
        "4. Read `DEVELOPMENT.md`, `ARCHITECTURE.md`, and `TESTING.md` before changing unfamiliar subsystems.",
        "5. Continue from the active work item unless the repository maintainer changes priorities.",
        "6. Keep public handoff files free of credentials, private infrastructure details, and conversation transcripts.",
        "",
    ])
    return "\n".join(lines)


def render_handoff(project, releases, active):
    latest = releases[0]
    canonical = canonical_repository(project)
    dev_branch = upstream_value(project, "upstream_development_branch", "development_branch")
    prerelease_branch = upstream_value(project, "upstream_prerelease_branch", "prerelease_branch")
    upstream_pr = project.get("upstream_pull_request", project.get("pull_request"))
    lines = [
        "# Torrent Dashboard Development Handoff",
        "",
        "> Generated from `release_notes/releases.json` and `development/current.json`. Do not edit this file manually.",
        "",
        "## Repository context",
        "",
        "This handoff is intentionally portable across public forks. Verify the current checkout's Git remote, branch, and open work before using upstream references as instructions.",
        "",
        f"- Canonical upstream: `{canonical}`",
        f"- Last documented upstream build: **v{latest['version']}** ({latest.get('status', 'unknown')})",
    ]
    if dev_branch:
        lines.append(f"- Upstream development branch: `{dev_branch}`")
    if prerelease_branch:
        lines.append(f"- Upstream prerelease branch: `{prerelease_branch}`")
    if upstream_pr:
        lines.append(f"- Upstream active PR: **#{upstream_pr}**")

    lines.extend([
        "",
        "## Last known-good state",
        "",
        latest["summary"],
        "",
        "The released-state details and recent history are in `PROJECT_STATE.md`; architectural constraints are in `ARCHITECTURE.md`.",
        "",
        "## Active development intent",
        "",
        f"- Status: **{active['status']}**",
        f"- Objective: **{active['objective']}**",
        f"- Why: {active['why']}",
    ])
    if active["acceptance_criteria"]:
        lines.extend(["", "### Acceptance criteria", "", bullets(active["acceptance_criteria"])])
    if active["decisions"]:
        lines.extend(["", "### Decisions already made", "", bullets(active["decisions"])])
    if active["files"]:
        lines.extend(["", "### Expected areas of change", "", bullets([f"`{item}`" for item in active["files"]])])
    if active["blockers"]:
        lines.extend(["", "### Blockers", "", bullets(active["blockers"])])
    else:
        lines.extend(["", "### Blockers", "", "None currently recorded."])
    if active["out_of_scope"]:
        lines.extend(["", "### Explicitly out of scope", "", bullets(active["out_of_scope"])])

    lines.extend([
        "",
        "## Exact next action",
        "",
        active["next_action"],
        "",
        "## Resume checklist",
        "",
        "1. Inspect `git remote -v`, `git status`, and the current branch/ref. In a fork, treat upstream branch/PR names only as historical context.",
        "2. Read `DEVELOPMENT.md`, `ARCHITECTURE.md`, `TESTING.md`, and any relevant records under `docs/decisions/`.",
        "3. Run `python release_tools/validate_source.py` before making changes so the inherited baseline is known-good.",
        "4. Compare the checkout against the last documented release and inspect any uncommitted or branch-only changes before continuing.",
        "5. Work from `development/current.json`; update it when scope, decisions, blockers, or the exact next action changes.",
        "6. Regenerate this file with `python release_tools/generate_release_notes.py --version <current-version>` whenever active work state changes.",
        "",
        "## Fork guidance",
        "",
        "- Forks may keep the canonical upstream reference for lineage while replacing `development/current.json` with their own active roadmap.",
        "- Forks are not required to use the upstream branch names, PR number, prerelease channel, or release cadence.",
        "- If a fork publishes its own updater releases, review its workflow branch triggers and configure Torrent Dashboard to use that fork's public release repository.",
        "- Do not place credentials, private network addresses, private incident details, or chat transcripts in public continuity files.",
        "",
    ])
    return "\n".join(lines)


def write_or_check(path: Path, content: str, check: bool):
    if check:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != content:
            raise RuntimeError(f"Generated file is stale: {path.relative_to(ROOT)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Generate Torrent Dashboard release and handoff notes")
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-body")
    parser.add_argument("--project-state", default="PROJECT_STATE.md")
    parser.add_argument("--handoff", default="HANDOFF.md")
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    project, releases = load_source()
    active = load_active_work()
    wanted = str(args.version).strip().lstrip("vV")
    release = next((item for item in releases if item["version"] == wanted), None)
    if not release:
        raise RuntimeError(f"No release metadata exists for v{wanted}")

    project_state = render_project_state(project, releases)
    handoff = render_handoff(project, releases, active)
    changelog = render_changelog(releases)
    write_or_check(ROOT / args.project_state, project_state, args.check)
    write_or_check(ROOT / args.handoff, handoff, args.check)
    write_or_check(ROOT / args.changelog, changelog, args.check)

    if args.release_body:
        path = ROOT / args.release_body
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_release_body(release), encoding="utf-8")

    print(f"Release metadata and handoff OK for v{wanted}")


if __name__ == "__main__":
    main()
