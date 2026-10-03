"""A whole fake host for the executor tests.

No test here touches the host, systemd, GitHub or a real spool. Every
external thing sits behind the small protocol the code already injects:

| Real thing | Fake |
|---|---|
| a child process | `FakeRun`, a recorded argv list with scripted answers |
| the spool | five directories under `tmp_path` |
| the GitHub API | `FakeApi`, one dict per path |
| the phone | a `Transport` function the test writes |
| `systemctl` | `FakeRun`'s answers for `is-active` and `NRestarts` |

The helpers build a host whose install roots, work root and unit directories
are all inside `tmp_path`, so a bug that escapes containment fails the test
rather than the machine.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_release.catalog import CATALOG, Releases, Repo
from agent_release.executor.approval import Decision, Summary, Verdict
from agent_release.executor.host import Command, Host
from agent_release.executor.host import Result as RunResult
from agent_release.executor.live_state import STAMP_FILE, Readers
from agent_release.executor.provenance import Accept, ApiReply
from agent_release.executor.spool import DONE_DIR, REJECTED_DIR, REQUESTS_DIR, RUNNING_DIR

#: Every fixture SHA is 40 lowercase hex, because the manifest's pattern is.
SHA_OF = {
    "chaperone": "2b59c3bd81f4a6079ce5d2a3418b6f0cc7d9e215",
    "attendance": "8c41d027fe5b3a9016d4e7c2b508af3196720de5",
    "caregiver": "4a7c2e19bd3f508c6e21af4b90d7c3516882ee40",
    "noticeboard": "1e9a640fb27c853d0a416ff2cb3d7905e8a12b64",
    "playpen": "5f0b73d1ca4e26987b3d0af5c81926e4ad70b3c1",
    "mcp-servers": "c0de4471b9a2f38e5d7016cc82ab4f90d3e61a58",
    "infra": "9d1f0c7a5b2e4438a6c0d19f37be5a2c48e1067b",
    "releasectl": "b6e2a90d47c1f3825ae0db6194c73f08251ad6e7",
    "registry-data": "77aa10c4e2b8936df05174c3ab29e6108dd4f3b2",
}

DIGEST = "sha256:" + "ab" * 32

#: A valid upper-case Crockford ULID, contract 06 §9's pattern.
REQUEST_ID = "01K5J8M2Q7V3X9R4T6N0B8C2DE"

PR_NUMBER = 41
HEAD_SHA = "3f21c9be04a75d1806e3fb92ac5107d4e6b28a19"


@dataclass
class FakeRun:
    """Every child the executor would start, recorded instead of run.

    `answers` maps a matching argv word to what the child prints. `fails`
    maps the same to an exit code, so a test makes exactly one command fail
    and leaves the rest working.
    """

    answers: dict[str, str] = field(default_factory=dict[str, str])
    fails: dict[str, int] = field(default_factory=dict[str, int])
    #: What a failing command's OWN stdout was, keyed the same as `fails`.
    #: `noticeboard-verify --json` writes its report to stdout and nothing to
    #: stderr even on exit 1, so a test that only scripted stderr could
    #: never reproduce a hook's report.
    fail_stdout: dict[str, str] = field(default_factory=dict[str, str])
    seen: list[Command] = field(default_factory=list[Command])
    #: Run when a command matches, so a test can act between two steps.
    hooks: dict[str, Callable[[Command], None]] = field(
        default_factory=dict[str, Callable[[Command], None]]
    )
    #: Consulted first. A fake that must answer differently per command —
    #: `git rev-parse`, which gives one SHA per checkout — lives here.
    dynamic: Callable[[Command], RunResult | None] | None = None

    def __call__(self, command: Command) -> RunResult:
        self.seen.append(command)
        if self.dynamic is not None:
            answered = self.dynamic(command)
            if answered is not None:
                return answered

        key = self._key(command)
        hook = self.hooks.get(key) if key else None
        if hook is not None:
            hook(command)

        if key is not None and key in self.fails:
            return RunResult(self.fails[key], self.fail_stdout.get(key, ""), f"{key} failed\n")

        return RunResult(0, self.answers.get(key or "", ""), "")

    def argv_lines(self) -> list[str]:
        return [" ".join(one.argv) for one in self.seen]

    def ran(self, word: str) -> bool:
        return any(word in line for line in self.argv_lines())

    def _key(self, command: Command) -> str | None:
        """The first scripted key any argv word CONTAINS.

        A substring, because `argv[0]` of a verify hook is an absolute path
        under `tmp_path` and a test names the hook, not the directory pytest
        happened to pick.
        """
        keys = sorted({*self.answers, *self.fails, *self.hooks})
        for word in command.argv:
            for key in keys:
                if key in word:
                    return key

        return None


NRESTARTS_WORD = "NRestarts"


def git_host_answers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """What systemd must print for a clean switch."""
    return {"is-active": "active", NRESTARTS_WORD: "0"} | (extra or {})


class GitFake:
    """Enough `git` for `install.fetch`, without a repository.

    `fetch` clones a root-owned checkout by SHA and then PROVES the work
    tree is at that SHA. A fake that always printed one answer would make
    that proof vacuous, so this one keeps a SHA per checked-out directory
    and `rev-parse HEAD` reads it back.

    `write_tree(directory, component)` is what the clone leaves behind: the
    test's `component.yaml` at the path the catalog gives that component.
    """

    def __init__(self, write_tree: Callable[[Path, str], None]) -> None:
        self.write_tree = write_tree
        self.heads: dict[str, str] = {}

    def __call__(self, command: Command) -> RunResult | None:
        argv = list(command.argv)
        if not argv or not argv[0].endswith("git"):
            return None

        if "clone" in argv:
            destination = Path(argv[-1])
            destination.mkdir(parents=True, exist_ok=True)
            self.write_tree(destination, destination.name)

            return RunResult(0, "", "")

        # `-C <path>` and not `argv[2]`: root puts its own `-c`
        # words in front of every verb, so a fixed index reads a config
        # word instead of the checkout it means.
        where = _dash_c(argv)
        if "checkout" in argv:
            self.heads[where] = argv[-1]

            return RunResult(0, "", "")

        if "rev-parse" in argv:
            return RunResult(0, f"{self.heads.get(where, '')}\n", "")

        return RunResult(0, "", "")


def _dash_c(argv: list[str]) -> str:
    """The directory `git -C` was given. `-c` (lower case) is a config word
    and never this, so the match is exact."""
    for index, word in enumerate(argv[:-1]):
        if word == "-C":
            return argv[index + 1]

    return ""


def fake_host(tmp_path: Path, run: FakeRun) -> Host:
    """A host whose every path sits under `tmp_path`."""
    made = (
        "work",
        "corpus",
        "system-units",
        "user-units",
        "sessions",
        "components",
        "state",
        "mcp-state",
    )
    for name in made:
        (tmp_path / name).mkdir(exist_ok=True)

    # The three corpus clones root reads. They exist here because
    # `require_trusted` refuses a source that is not a git checkout
    # this account owns, and a fake host that had none would refuse every
    # release for the one reason no test is about.
    for repo in Repo:
        (tmp_path / "corpus" / str(repo) / ".git").mkdir(parents=True, exist_ok=True)

    return Host(
        run=run,
        clock=_ticking_clock(),
        sleep=lambda _: None,
        work_root=tmp_path / "work",
        source_root=tmp_path / "corpus",
        # The test user owns everything under `tmp_path`, so this is the
        # same statement `drain.build_wiring` makes about the operator on the host:
        # the corpus belongs to the account the requester runs as
        # (`executor/source.py`).
        source_owner_uid=os.getuid(),
        source_owner_gid=os.getgid(),
        system_unit_dir=tmp_path / "system-units",
        user_unit_dir=tmp_path / "user-units",
        sessions_root=tmp_path / "sessions",
        install_roots=(tmp_path / "components",),
        # The roster root writes at the end of an `mcp-servers` release.
        # Without it here, any test that switches that
        # component writes into the real `/srv/agents/state/rework/`.
        roster_file=tmp_path / "state" / "upstreams.yaml",
        # Contract 01b §4.2's root, which the visit makes before any
        # release runs. Here so a server that keeps state
        # never reaches for the real `/var/lib/agent-mcp`.
        mcp_state_root=tmp_path / "mcp-state",
    )


def _ticking_clock() -> Callable[[], float]:
    """A clock that never repeats, so a `seconds` field is not always 0.0
    and a wait loop cannot spin forever on a frozen time."""
    state = {"now": 1758153600.0}

    def clock() -> float:
        state["now"] += 0.1

        return state["now"]

    return clock


def make_spool_dirs(tmp_path: Path) -> Path:
    """The five spool entries, with the lock file the executor opens."""
    root = tmp_path / "spool"
    for name in (REQUESTS_DIR, RUNNING_DIR, DONE_DIR, REJECTED_DIR):
        (root / name).mkdir(parents=True, exist_ok=True)

    (root / "lock").write_bytes(b"")

    return root


def write_request(spool_root: Path, request_id: str, body: dict[str, object]) -> Path:
    """One request file, written the way a requester writes it."""
    path = spool_root / REQUESTS_DIR / f"{request_id}.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    return path


def request_body(
    components: dict[str, str],
    *,
    request_id: str = REQUEST_ID,
    kind: str = "release",
    requested_by: str = "agent-control",
) -> dict[str, object]:
    """§2.3's seven fields, all valid. A test breaks exactly one."""
    return {
        "id": request_id,
        "kind": kind,
        "components": components,
        "rollback_of": None,
        "requested_by": requested_by,
        "requester_session": "tui-01K5J7Z9R0P2M4C6H8K1N3V5W7",
        "ts": 1758153590.0,
    }


def live_state_body(live: dict[str, str | None], latest: dict[str, str]) -> dict[str, object]:
    """Contract 06 §11's document, with facts for every catalog component.

    Root never READS one of these: it builds its own, and
    `fake_readers` is what a test scripts instead. This stays for the one
    door a document still comes through — `agent-releasectl resolve
    --state <file>`, which hands the PURE resolver a fixture.
    """
    facts = {
        row.name: {"sha": SHA_OF[row.name], "input_digest": DIGEST, "artifact_digest": None}
        for row in CATALOG
    }

    return {"live": live, "provided": {}, "latest": latest, "facts": facts}


def write_live_state(tmp_path: Path, body: dict[str, object]) -> Path:
    path = tmp_path / "live-state.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    return path


def fake_readers(
    latest: dict[str, str] | None = None, known: dict[str, str] | None = None
) -> Readers:
    """The two answers root gets off the host: what `latest` names, and what
    commit a tag is.

    `latest` is the tag list GitHub would answer with, per component.
    `known` overrides a component's SHA; everything else resolves to
    `SHA_OF`, which is the fixture set every other module here uses. The
    digest is one constant: what it is made of is `source.py`'s business and
    a scripted reader proving a hash would prove the script.
    """
    versions = dict(latest or {})
    shas = SHA_OF | dict(known or {})

    def newest(component: str) -> str | None:
        return versions.get(component)

    def tag_sha(component: str, version: str) -> str | None:
        del version

        return shas.get(component)

    def digest(component: str, sha: str) -> str | None:
        del component, sha

        return DIGEST

    return Readers(newest_version=newest, tag_sha=tag_sha, input_digest=digest)


#: The one directory that makes a tree a venv. `uv venv` always writes it,
#: and `selfcontained.escapes` refuses a `kind: venv` tree without one: a
#: build that wrote no site-packages wrote no environment, and a walk that
#: found nothing must not read as a pass.
SITE_PACKAGES = Path("lib") / "python3.12" / "site-packages"


def stamp_tree(root: Path, name: str, version: str) -> Path:
    """An install tree as the switch leaves it: a bin directory, a hook, a
    site-packages and the version stamp `live_state` reads back.

    The hook is named for the COMPONENT, so `chaperone.new` and `chaperone.prev` both
    carry `chaperone-verify` — which is what the manifest names and what the
    executor relocates into the staged tree.

    The site-packages is EMPTY, which is what self-contained looks like: no
    `.pth` names a path outside the tree and no `direct_url.json` says
    editable (contract 06 §8.2). A test that wants the fault seen on the host writes
    the `.pth` itself.
    """
    tree = root / name
    component = name.removesuffix(".new").removesuffix(".prev")
    (tree / "bin").mkdir(parents=True, exist_ok=True)
    (tree / SITE_PACKAGES).mkdir(parents=True, exist_ok=True)
    hook = tree / "bin" / f"{component}-verify"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    (tree / STAMP_FILE).write_text(f"{version}\n", encoding="utf-8")

    return tree


@dataclass
class FakeApi:
    """The GitHub answers P1 to P5 read, all green unless a test edits one."""

    bodies: dict[str, object]
    asked: list[str] = field(default_factory=list[str])

    def __call__(self, path: str, accept: Accept) -> ApiReply:
        del accept
        self.asked.append(path)
        for prefix, body in self.bodies.items():
            if path.startswith(prefix):
                return ApiReply(200, body)

        return ApiReply(404, "not found")


def green_api(repo: str, tag: str, sha: str) -> FakeApi:
    """Every provenance answer a merged, CI-green, bot-cut Release gives."""
    base = f"/repos/example-owner/{repo}"

    return FakeApi(
        {
            f"{base}/releases/tags/{tag}": {
                "tag_name": tag,
                "draft": False,
                "prerelease": False,
                "author": {"login": "github-actions[bot]", "type": "Bot"},
            },
            f"{base}/git/ref/tags/{tag}": {
                "ref": f"refs/tags/{tag}",
                "object": {"type": "commit", "sha": sha},
            },
            f"{base}/compare/main...{sha}": {"status": "behind"},
            f"{base}/commits/{sha}/pulls": [
                {
                    "number": PR_NUMBER,
                    "merged_at": "2026-09-19T10:00:00Z",
                    "base": {"ref": "main"},
                    "merge_commit_sha": sha,
                    "head": {"sha": HEAD_SHA},
                }
            ],
            f"{base}/actions/runs": {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "name": "ci",
                        "head_sha": HEAD_SHA,
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
            },
        }
    )


def grant_transport(
    at: float = 1758153712.0,
) -> Callable[[str, str, Summary, float], Decision]:
    """A phone that taps yes for whatever gate it is shown."""

    def transport(action_id: str, gate: str, summary: Summary, wait_s: float) -> Decision:
        del action_id, summary, wait_s

        return Decision(Verdict.GRANTED, gate, at)

    return transport


def releasable_catalog_names() -> list[str]:
    return [row.name for row in CATALOG if row.releases is Releases.YES]


def this_uid() -> int:
    """The spool's owner in a test is whoever runs pytest."""
    return os.getuid()
