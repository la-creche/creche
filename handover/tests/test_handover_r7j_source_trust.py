"""What root reads a repository out of, and on what terms.

The hazard this file pins: an `Installer.fetch` that runs git AS ROOT
inside `/srv/agents/work/platform/<repo>`, which is the `agent-control`
family's READ-WRITE mount (the registry's `families/agent-control/family.yaml`,
`docs/rework/spec.md` §4.4). A model in that sandbox writes `.git/config`,
`.git/hooks` and `.git/objects/info/alternates` of all three checkouts. The
SHA pins WHAT root builds. It does not pin what root's `git` process DOES
while it reads a repository whose configuration an attacker wrote.

Three things are proved here, and the first one is proved with REAL git,
because a fake `run` would stand exactly where the thing under test stands:

1. A hostile `.git/config` in the SOURCE — an `fsmonitor` command, a
   `core.pager`, an `[include]`, an alias, `uploadpack.packObjectsHook` — and
   a hostile `.git/hooks` directory run NOTHING when root fetches. Each
   hostile setting writes its own marker file and the test looks for all of
   them.
2. Root REFUSES a source it should not trust BEFORE any git process starts in
   it. The refusal has its own code.
3. The refusal for a SHA the corpus does not hold names the one command that
   fixes it.

Measured on this Mac with git 2.54.0 (Apple Git-157), nothing as root, no ssh
and no host. `executor/source.py` records what each git setting
really does.
"""

from __future__ import annotations

import ast
import os
import stat
import subprocess
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.host import As, Host, Result, make_runner
from handover.executor.install import Installer, StepFailed
from handover.executor.source import (
    CORPUS_ROOT,
    REFRESH_COMMAND,
    git_env,
    git_ground,
    require_trusted,
)
from handover_executor_fixtures import FakeRun, GitFake, fake_host

GIT = "/usr/bin/git"

#: One marker per hostile setting. A setting that RAN leaves its file behind.
MARKERS = (
    "fsmonitor",
    "pager",
    "sshcommand",
    "editor",
    "askpass",
    "packobjectshook",
    "included",
    "hook_post-checkout",
    "hook_post-index-change",
    "hook_reference-transaction",
    "hook_pre-commit",
)


def _git(*args: str, cwd: Path | None = None) -> None:
    done = subprocess.run(
        [GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.fixture
def hostile(tmp_path: Path) -> dict[str, object]:
    """A real repository an attacker in the sandbox has finished with.

    Every hostile setting names a command that writes its own marker, so the
    assertion below is "no marker exists" and not "the clone looked fine".
    """
    markers = tmp_path / "markers"
    markers.mkdir()
    source = tmp_path / "corpus" / "agent-control"
    source.mkdir(parents=True)
    _git("init", "-q", "-b", "main", ".", cwd=source)
    _git("config", "user.email", "t@example.com", cwd=source)
    _git("config", "user.name", "t", cwd=source)
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("commit", "-qm", "one", cwd=source)
    sha = subprocess.run(
        [GIT, "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True
    ).stdout.strip()

    included = tmp_path / "included.gitconfig"
    included.write_text(
        f'[core]\n\taskPass = "touch {markers}/askpass"\n'
        f'[uploadpack]\n\tpackObjectsHook = "touch {markers}/included"\n',
        encoding="utf-8",
    )
    hooks = source / ".git" / "hostilehooks"
    hooks.mkdir()
    with (source / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write(
            "[core]\n"
            f'\tfsmonitor = "touch {markers}/fsmonitor"\n'
            f'\tpager = "touch {markers}/pager; cat"\n'
            f"\thooksPath = {hooks}\n"
            f'\tsshCommand = "touch {markers}/sshcommand"\n'
            f'\teditor = "touch {markers}/editor"\n'
            "[alias]\n"
            f'\trp = "!touch {markers}/alias; true"\n'
            "[include]\n"
            f"\tpath = {included}\n"
            "[uploadpack]\n"
            f'\tpackObjectsHook = "touch {markers}/packobjectshook"\n'
        )

    for name in ("post-checkout", "pre-commit", "post-index-change", "reference-transaction"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch {markers}/hook_{name}\n", encoding="utf-8")
        hook.chmod(0o755)

    return {"source": source, "sha": sha, "markers": markers, "root": tmp_path / "corpus"}


def _real_host(tmp_path: Path, corpus: Path) -> Host:
    """A host whose `run` starts REAL children, as this user.

    `make_runner` runs an `As.ROOT` command directly — `runuser` is only for
    the other two identities — so this is the exact argv, the exact
    environment and the exact git the executor would use on the host.
    """
    return Host(
        run=make_runner(os.getuid()),
        work_root=tmp_path / "work",
        source_root=corpus,
        source_owner_uid=os.getuid(),
        source_owner_gid=os.getgid(),
        install_roots=(tmp_path / "components",),
    )


# ---- 1. a hostile source runs nothing --------------------------------------


def test_a_hostile_source_config_runs_nothing_when_root_fetches(
    hostile: dict[str, object], tmp_path: Path
) -> None:
    """The whole fetch, real git, hostile source, and not one marker."""
    host = _real_host(tmp_path, Path(str(hostile["root"])))
    into = tmp_path / "work" / "01" / "agent-control"

    Installer(host).fetch("agent-control", str(hostile["sha"]), into)

    markers = Path(str(hostile["markers"]))
    left = sorted(one.name for one in markers.iterdir())
    assert left == [], left
    assert (into / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_the_fetched_tree_is_at_the_resolved_sha(
    hostile: dict[str, object], tmp_path: Path
) -> None:
    host = _real_host(tmp_path, Path(str(hostile["root"])))
    into = tmp_path / "work" / "01" / "agent-control"

    Installer(host).fetch("agent-control", str(hostile["sha"]), into)

    head = subprocess.run(
        [GIT, "rev-parse", "HEAD"], cwd=into, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head == str(hostile["sha"])


def test_the_clone_does_not_inherit_the_sources_alternates(
    hostile: dict[str, object], tmp_path: Path
) -> None:
    """Measured on git 2.54.0: `clone --local` copies the SOURCE's
    `.git/objects/info/alternates` verbatim into the clone, so root's object
    store would point wherever the attacker wrote. The trust check refuses
    such a source outright, which is what this asserts."""
    source = Path(str(hostile["source"]))
    alternates = source / ".git" / "objects" / "info" / "alternates"
    alternates.write_text("/etc\n", encoding="utf-8")
    host = _real_host(tmp_path, Path(str(hostile["root"])))

    with pytest.raises(Refusal) as raised:
        Installer(host).fetch("agent-control", str(hostile["sha"]), tmp_path / "work" / "x")

    assert raised.value.code is RefusalCode.SOURCE
    assert "alternates" in raised.value.detail


# ---- 2. the trust rule -----------------------------------------------------


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    repo = root / "agent-control"
    (repo / ".git").mkdir(parents=True)

    return root


def test_a_trusted_source_passes(tmp_path: Path) -> None:
    root = _corpus(tmp_path)

    require_trusted("agent-control", root / "agent-control", os.getuid(), os.getgid())


def test_a_source_owned_by_a_third_account_is_refused(tmp_path: Path) -> None:
    root = _corpus(tmp_path)

    with pytest.raises(Refusal) as raised:
        require_trusted("agent-control", root / "agent-control", os.getuid() + 1, os.getgid())

    assert raised.value.code is RefusalCode.SOURCE
    assert "owner" in raised.value.detail


def test_a_world_writable_source_is_refused(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    repo = root / "agent-control"
    repo.chmod(0o777)

    with pytest.raises(Refusal) as raised:
        require_trusted("agent-control", repo, os.getuid(), os.getgid())

    assert raised.value.code is RefusalCode.SOURCE
    assert "other" in raised.value.detail


def test_a_group_writable_source_passes_only_for_the_owners_own_group(tmp_path: Path) -> None:
    """`/srv/agents/code/<repo>` is the operator's, group and owner, mode 0775
    on the host today, and no sandbox user is in that group. Group write is therefore allowed for
    the owner's OWN group and for no other."""
    root = _corpus(tmp_path)
    repo = root / "agent-control"
    repo.chmod(0o775)

    require_trusted("agent-control", repo, os.getuid(), os.getgid())

    with pytest.raises(Refusal) as raised:
        require_trusted("agent-control", repo, os.getuid(), os.getgid() + 1)

    assert "group" in raised.value.detail


def test_a_world_writable_git_directory_is_refused(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    repo = root / "agent-control"
    (repo / ".git").chmod(0o777)

    with pytest.raises(Refusal) as raised:
        require_trusted("agent-control", repo, os.getuid(), os.getgid())

    assert ".git" in raised.value.detail


def test_a_symlinked_source_is_refused(tmp_path: Path) -> None:
    root = _corpus(tmp_path)
    (root / "agent-mcp").symlink_to(root / "agent-control")

    with pytest.raises(Refusal) as raised:
        require_trusted("agent-mcp", root / "agent-mcp", os.getuid(), os.getgid())

    assert raised.value.code is RefusalCode.SOURCE


def test_a_missing_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Refusal) as raised:
        require_trusted("agent-mcp", tmp_path / "corpus" / "agent-mcp", os.getuid(), os.getgid())

    assert raised.value.code is RefusalCode.SOURCE


def test_a_git_file_instead_of_a_git_directory_is_refused(tmp_path: Path) -> None:
    """A `.git` FILE names a gitdir somewhere else, and that directory is not
    the one this check just read."""
    root = tmp_path / "corpus"
    repo = root / "agent-control"
    repo.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /tmp/elsewhere\n", encoding="utf-8")

    with pytest.raises(Refusal) as raised:
        require_trusted("agent-control", repo, os.getuid(), os.getgid())

    assert raised.value.code is RefusalCode.SOURCE


def test_the_refusal_names_no_path_it_did_not_validate(tmp_path: Path) -> None:
    """§3.2 rule 4: a refusal's detail is built from the component NAME and
    fixed words, never from a path an attacker chose."""
    root = _corpus(tmp_path)
    (root / "agent-control").chmod(0o777)

    with pytest.raises(Refusal) as raised:
        require_trusted("agent-control", root / "agent-control", os.getuid(), os.getgid())

    assert str(tmp_path) not in raised.value.detail


# ---- the check runs before any child -------------------------------------


def test_root_starts_no_child_in_a_source_it_refuses(tmp_path: Path) -> None:
    run = FakeRun()
    host = fake_host(tmp_path, run)
    (host.source_root / "agent-control" / ".git").mkdir(parents=True, exist_ok=True)
    (host.source_root / "agent-control").chmod(0o777)

    with pytest.raises(Refusal):
        Installer(host).fetch("agent-control", "0" * 40, tmp_path / "work" / "x")

    assert run.seen == []


# ---- 3. the ground, and the freshness refusal ------------------------------


def test_the_ground_names_every_setting_that_can_run_a_command() -> None:
    words = git_ground(Path("/srv/agents/code/agent-control"))
    text = " ".join(words)

    assert "safe.directory=/srv/agents/code/agent-control" in text
    # Measured: `-c safe.directory=<worktree>` unlocks `cat-file` and does
    # NOT unlock `clone --local`, which checks the GITDIR.
    assert "safe.directory=/srv/agents/code/agent-control/.git" in text
    for setting in (
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "core.pager=cat",
        "core.sshCommand=false",
        "core.editor=false",
        "core.askPass=",
        "protocol.ext.allow=never",
    ):
        assert setting in text
    assert all(word == "-c" for word in words[::2])


def test_the_git_environment_removes_every_config_root_did_not_write() -> None:
    env = git_env()

    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_every_git_call_of_a_fetch_carries_the_ground(tmp_path: Path) -> None:
    git = GitFake(lambda directory, _: None)
    run = FakeRun(dynamic=git)
    host = fake_host(tmp_path, run)
    (host.source_root / "agent-control" / ".git").mkdir(parents=True, exist_ok=True)

    Installer(host).fetch("agent-control", "a" * 40, tmp_path / "work" / "x")

    assert run.seen, "the fetch started no child at all"
    for command in run.seen:
        assert command.identity is As.ROOT
        assert "-c" in command.argv, command.argv
        assert "core.hooksPath=/dev/null" in command.argv, command.argv
        assert dict(command.env)["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_a_sha_the_corpus_does_not_hold_names_the_command_that_fixes_it(
    hostile: dict[str, object], tmp_path: Path
) -> None:
    """The corpus is refreshed hourly, so a release right after a merge names
    a SHA it may not hold yet. The refusal has to say what the operator runs."""
    host = _real_host(tmp_path, Path(str(hostile["root"])))

    with pytest.raises(StepFailed) as raised:
        Installer(host).fetch("agent-control", "b" * 40, tmp_path / "work" / "x")

    assert REFRESH_COMMAND in raised.value.detail


def test_the_corpus_root_is_the_hosts_own_clones() -> None:
    """Contract 01 §5.5 rule 6's platform mirror: the operator's OWN clones, which
    every sandbox sees read-only. NOT `/srv/agents/work/platform`, which is
    the `agent-control` family's rw mount."""
    assert CORPUS_ROOT == "/srv/agents/code"
    assert Host(run=lambda _: Result(0)).source_root == Path(CORPUS_ROOT)


def test_an_unconfigured_host_trusts_only_root(tmp_path: Path) -> None:
    """The fail-closed default. A `Host` nobody gave an owner to refuses
    every operator-owned clone rather than trusting whatever it finds."""
    plain = Host(run=lambda _: Result(0))

    assert plain.source_owner_uid == 0
    assert plain.source_owner_gid == 0


def test_no_module_of_the_release_package_still_reads_the_platform_root() -> None:
    """The MCP path reads `mcp/<name>/server.yaml` too
    (`stage7-releases.md` §7.5), and out of the same rw mount it would
    have the same fault.

    Read with `ast` and not with a grep: every module that argues about the
    platform root still NAMES it in a docstring, which is the point. What
    must not exist is a string the code can hand to a path.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "handover"
    for module in package.rglob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue

            if id(node) in docstrings:
                continue

            assert "/srv/agents/work/platform" not in node.value, f"{module.name}:{node.lineno}"


def test_the_source_directory_mode_helper_reads_the_real_bits(tmp_path: Path) -> None:
    """A guard on the guard: `require_trusted` must read `lstat`, so a
    symlink is the link's own mode and not its target's."""
    root = _corpus(tmp_path)
    repo = root / "agent-control"

    assert stat.S_ISDIR(os.lstat(repo).st_mode)
