#!/usr/bin/env python3
"""OPERATOR — on the host, as the operator: /opt/creche/bin/docs-mirror.py [--dry-run]
(run by the user timer that bin/setup-docs-mirror.sh installs)

Mirror the repos' docs into the vault, organized for reading in Obsidian.

One-way, and that is the whole design: git stays the sole source of truth, the
vault copy is a rendering surface, and any edit made to the mirror converges
back to git on the next run. LiveSync then fans the folder out to every device,
so the phone gets the docs without running git anywhere.

Reads the corpus clones (`/srv/agents/code`, kept fresh by code-corpus-sync)
rather than the network: one puller, many readers. Filenames and relative
paths are preserved verbatim so intra-doc links keep working; the curated
layer is generated around the files — a Home note, an index per repo with
aliases and one-line blurbs, and injected frontmatter carrying provenance and
`obsidianUIMode: preview` for the read-mode plugins.

Churn matters: LiveSync re-uploads any changed file to CouchDB and every
device, and the memory service extracts each changed note again with the LLM.
So nothing here embeds a run timestamp or the repo's HEAD. Each doc carries
the last commit that changed it, each index and the Home note the newest
commit that changed a mirrored doc. Files are rewritten only when their bytes
differ, so a commit that changes no doc writes nothing at all.

ALLOWLIST, not denylist: only `docs/**` and each repo's top-level
`README.md` are mirrored. This mirror feeds an internet-facing
CouchDB via LiveSync and lands on every phone, so it never carries an
`AGENTS.md`, `ha/**`, `infra/**`, `bin/**`, or an agent's `instructions.md`
— the LAN topology, secret key NAMES and where they live, the sudoers path,
the webhook naming scheme, and the full system prompt of every agent in the
fleet. None of that is "documentation a person reads"; all of it is a ready-
made attack map for anyone who compromises the CouchDB credential or finds a
lost phone. Anything under `docs/` that looks like an audit/review/postmortem
is excluded too — those are reports, not docs. See `is_allowed()`.

    DOCS_MIRROR_CORPUS=/srv/agents/code   override for testing
    DOCS_MIRROR_TARGET=/srv/agents/vault/docs
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

CORPUS = Path(os.environ.get("DOCS_MIRROR_CORPUS", "/srv/agents/code"))
TARGET = Path(os.environ.get("DOCS_MIRROR_TARGET", "/srv/agents/vault/docs"))

HOME_NOTE = "Home.md"
ABOUT_NOTE = "About this mirror.md"
INDEX_NOTE = "_index.md"

FRONTMATTER_OPEN = "---"

#: One curated entry: path inside the repo, the alias a human reads, one line
#: on what it is. Anything present in the repo but not listed lands in the
#: index's "Everything else" tail, so new docs appear without a code change —
#: listing them here is how they get a proper name.
Entry = tuple[str, str, str]

#: repo -> ordered (section heading, entries). Paths are repo-relative.
SECTIONS: dict[str, list[tuple[str, list[Entry]]]] = {
    "creche": [
        (
            "Operating",
            [
                ("README.md", "Root README", "Start here: the services and the layout"),
            ],
        ),
    ],
    "mcp": [
        (
            "The MCP distribution",
            [
                ("README.md", "README", "One distribution, one binary per server, zero deps"),
            ],
        ),
    ],
    "registry": [
        (
            "The registry",
            [
                ("README.md", "README", "Definitions as data; the git log is the audit trail"),
            ],
        ),
    ],
    "chaperone": [
        (
            "The chaperone",
            [
                ("README.md", "README", "A policy layer between an agent app and its tools"),
            ],
        ),
    ],
}

#: Repo-relative name fragments that mark a `docs/*.md` file as a report or
#: audit, not documentation — checked case-insensitively against
#: the filename only, so a real doc named e.g. "code-review-guide.md" is not
#: caught by accident (see `is_allowed`, which matches on the full stem).
REPORT_LIKE = ("fleet-review-", "audit", "review", "postmortem")


def is_allowed(rel: Path) -> bool:
    """Allowlist, not denylist: docs/** and top-level README.md.

    Everything else — every `AGENTS.md`, `ha/**`, `infra/**`, `bin/**`, every
    agent's `instructions.md` — is infrastructure, not documentation, and this
    mirror's destination (an internet-facing CouchDB, and every phone) is not
    where it belongs.
    """
    if rel == Path("README.md"):
        return True
    if rel.parts[0] != "docs":
        return False
    # Every path part, not just the stem: docs/reviews/<file> is a report too.
    # A report in a directory that carries no marker keeps one in its own
    # NAME, because the mirror's destination is internet-facing.
    parts = [part.lower() for part in rel.with_suffix("").parts[1:]]
    return not any(marker in part for part in parts for marker in REPORT_LIKE)


ABOUT_BODY = """\
This folder is a **read-only mirror**. The originals live in git
(creche, mcp, registry, chaperone); an hourly timer on Hyperion
regenerates everything here from the latest commits, and LiveSync carries it
to your devices.

Two consequences, both deliberate:

- **Edits here die.** Anything you change in this folder is overwritten on
  the next sync. If a doc is wrong, fix it in its repo — the `source` line in
  each note's properties says exactly where it came from.
- **Reading view is the intended mode.** Every mirrored note carries
  `obsidianUIMode: preview`; install one of the read-mode plugins (see the
  operator's notes on this mirror) and this whole folder
  opens read-only.

New docs added to a repo appear here automatically under "Everything else" in
that repo's index; giving one a proper name and blurb is a one-line edit to
`SECTIONS` in `bin/docs-mirror.py`.
"""


class Fatal(Exception):
    """Something the operator has to fix before this can continue."""


def say(msg: str) -> None:
    print(f"[docs-mirror] {msg}")


def last_commit(repo_dir: Path, paths: list[Path]) -> str:
    """The short sha of the newest commit that changed one of `paths`, or
    'unknown' for a tree without git or paths that no commit holds.

    Not HEAD: on 2026-11-15 the HEAD sha in every file rewrote 22 docs at
    each merge to creche, and the memory service extracted each one again."""
    if not paths:
        # With no path, `git log` names HEAD.
        return "unknown"

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "-1", "--format=%h", "--", *map(str, paths)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def wanted_files(repo_dir: Path, repo: str) -> list[Path]:
    """Every doc the mirror carries for one repo, repo-relative, sorted.

    Hidden directories (.git, .venv, caches) are skipped wholesale — a vendored
    LICENSE.md is not documentation. `repo` is unused beyond that hidden-dir
    scan (the allowlist in `is_allowed` is repo-agnostic by design: `docs/**`
    and a top-level `README.md` mean the same thing in every repo here).
    """
    del repo  # kept in the signature: callers pass it, and it may matter again
    out: list[Path] = []
    for path in repo_dir.rglob("*.md"):
        rel = path.relative_to(repo_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if not is_allowed(rel):
            continue
        out.append(rel)
    return sorted(out)


def frontmatter(repo: str, rel: Path, commit: str) -> str:
    return "\n".join(
        [
            FRONTMATTER_OPEN,
            f"source: {repo}/{rel}",
            f"commit: {commit}",
            "mirror: read-only — regenerated from git; edits here are overwritten",
            "obsidianUIMode: preview",
            FRONTMATTER_OPEN,
            "",
            "",
        ]
    )


def mirrored(repo: str, rel: Path, body: str, commit: str) -> str:
    """A doc as the mirror renders it: provenance on top, text verbatim.

    A file that already opens with frontmatter keeps it untouched — merging
    two YAML blocks is how a mirror corrupts a doc, and none of ours carry any
    today.
    """
    if body.startswith(FRONTMATTER_OPEN):
        return body
    return frontmatter(repo, rel, commit) + body


def note_link(from_dir: Path, to_rel: Path, alias: str) -> str:
    """A standard relative markdown link, which Obsidian resolves and graphs.

    Relative rather than vault-absolute on purpose: it keeps working no matter
    where the vault root sits on a device.
    """
    rel = os.path.relpath(to_rel, from_dir)
    return f"[{alias}]({rel.replace(os.sep, '/')})"


def render_index(repo: str, files: list[Path], commit: str) -> str:
    """One repo's reading index: curated sections, then everything unlisted."""
    here = Path(repo)
    lines = [
        FRONTMATTER_OPEN,
        f"source: generated from {repo}@{commit}",
        "mirror: read-only — regenerated from git; edits here are overwritten",
        "obsidianUIMode: preview",
        FRONTMATTER_OPEN,
        "",
        f"# {repo}",
        "",
        f"Mirrored from `{repo}`; its newest doc change is `{commit}`. Files keep",
        "their repo paths so links inside them keep working; this index is the",
        "readable layer.",
    ]

    listed: set[str] = set()
    for heading, entries in SECTIONS.get(repo, []):
        section: list[str] = []
        for rel_str, alias, blurb in entries:
            if Path(rel_str) not in set(files):
                continue
            listed.add(rel_str)
            link = note_link(here, here / rel_str, alias)
            section.append(f"- {link} — {blurb}")
        if section:
            lines += ["", f"## {heading}", "", *section]

    tail = [rel for rel in files if str(rel) not in listed]
    if tail:
        lines += ["", "## Everything else", ""]
        lines += [f"- {note_link(here, here / rel, str(rel))}" for rel in tail]

    lines.append("")
    return "\n".join(lines)


def render_home(commits: dict[str, str]) -> str:
    lines = [
        FRONTMATTER_OPEN,
        "mirror: read-only — regenerated from git; edits here are overwritten",
        "obsidianUIMode: preview",
        FRONTMATTER_OPEN,
        "",
        "# Docs",
        "",
        "Read-only mirror of the project documentation, refreshed hourly from",
        f"git. What that means: [About this mirror]({ABOUT_NOTE.replace(' ', '%20')}).",
        "",
        "## Projects",
        "",
    ]
    for repo, commit in commits.items():
        lines.append(f"- [{repo}]({repo}/{INDEX_NOTE}) — at `{commit}`")
    lines += [
        "",
        "## Start here",
        "",
        "- [Root README](creche/README.md) — the services and the layout",
        "",
    ]
    return "\n".join(lines)


def build() -> dict[Path, str]:
    """Render the whole mirror in memory: target-relative path -> content."""
    if not CORPUS.is_dir():
        raise Fatal(f"corpus root {CORPUS} does not exist — is code-corpus-sync set up?")

    out: dict[Path, str] = {}
    commits: dict[str, str] = {}
    for repo in SECTIONS:
        repo_dir = CORPUS / repo
        if not repo_dir.is_dir():
            say(f"! corpus has no {repo} — add it to /srv/agents/state/code-repos.txt")
            continue
        files = wanted_files(repo_dir, repo)
        commit = last_commit(repo_dir, files)
        commits[repo] = commit

        for rel in files:
            body = (repo_dir / rel).read_text(encoding="utf-8", errors="replace")
            out[Path(repo) / rel] = mirrored(repo, rel, body, last_commit(repo_dir, [rel]))
        out[Path(repo) / INDEX_NOTE] = render_index(repo, files, commit)
        say(f"{repo}@{commit}: {len(files)} docs")

    if not commits:
        raise Fatal("no repos mirrored; nothing to publish")

    out[Path(HOME_NOTE)] = render_home(commits)
    out[Path(ABOUT_NOTE)] = (
        f"{FRONTMATTER_OPEN}\n"
        "mirror: read-only — regenerated from git; edits here are overwritten\n"
        "obsidianUIMode: preview\n"
        f"{FRONTMATTER_OPEN}\n\n# About this mirror\n\n" + ABOUT_BODY
    )
    return out


def publish(rendered: dict[Path, str]) -> tuple[int, int]:
    """Converge the target onto the rendered tree, touching as little as possible.

    Unchanged files are left alone (LiveSync uploads anything rewritten), and
    managed-but-no-longer-rendered .md files are deleted — that convergence is
    the read-only guarantee. Non-markdown files are never touched.
    """
    TARGET.mkdir(parents=True, exist_ok=True)

    written = 0
    for rel, content in rendered.items():
        dest = TARGET / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.read_text(encoding="utf-8", errors="replace") == content:
            continue
        dest.write_text(content, encoding="utf-8")
        written += 1

    keep = set(rendered)
    removed = 0
    for path in sorted(TARGET.rglob("*.md")):
        if path.relative_to(TARGET) not in keep:
            path.unlink()
            removed += 1

    for path in sorted(TARGET.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()

    return written, removed


def plan(rendered: dict[Path, str]) -> tuple[list[Path], list[Path]]:
    """What `publish` would change, without touching the filesystem.

    Mirrors `publish`'s own diff logic so `--dry-run` reports exactly what a
    real run would do, including every path the allowlist removes.
    """
    to_write: list[Path] = []
    for rel, content in rendered.items():
        dest = TARGET / rel
        if not dest.exists() or dest.read_text(encoding="utf-8", errors="replace") != content:
            to_write.append(rel)

    keep = set(rendered)
    to_remove: list[Path] = []
    if TARGET.is_dir():
        for path in sorted(TARGET.rglob("*.md")):
            rel = path.relative_to(TARGET)
            if rel not in keep:
                to_remove.append(rel)

    return sorted(to_write), to_remove


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written/removed under the current allowlist; touch nothing",
    )
    args = parser.parse_args(argv)

    rendered = build()
    if args.dry_run:
        to_write, to_remove = plan(rendered)
        for rel in to_write:
            say(f"would write: {rel}")
        for rel in to_remove:
            say(f"would remove: {rel}")
        say(f"dry run: {len(to_write)} would be written, {len(to_remove)} would be removed")
        return 0

    written, removed = publish(rendered)
    say(f"target {TARGET}: {written} written, {removed} removed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Fatal as exc:
        print(f"[docs-mirror] FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
