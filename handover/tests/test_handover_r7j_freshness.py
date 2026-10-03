"""The corpus is an hour behind, and the release is filed two minutes after
the merge.

`code-corpus-sync.timer` runs hourly, so `/srv/agents/code/<repo>` routinely
does not hold the SHA a fresh release resolves to. Three places answer that,
and each answers it in the way its own account can:

| Who | Runs as | What it does |
|---|---|---|
| `handover request` | the operator | fetches the component's repo, then files |
| the PEP's `release` verb | `chaperone` | runs NO git, and says so in its answer |
| the executor | root | refuses, naming the one command that fixes it |

The executor's half is in `test_handover_r7j_source_trust.py`. This file is
the other two, plus the rule that keeps them apart: `handover.corpus`
starts children, so the PEP must never import it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from handover.catalog import CATALOG, Repo
from handover.corpus import (
    FETCH_NOTE,
    CorpusReport,
    refresh,
    repos_of,
)
from handover.executor.source import CORPUS_ROOT
from handover_fixtures import manifest_text, write_manifest

from handover import cli
from handover import corpus as corpus_module

GIT = "/usr/bin/git"


def _make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main", "."),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
    ):
        subprocess.run([GIT, *args], cwd=repo, capture_output=True, check=True, timeout=120)

    (repo / "README.md").write_text("one\n", encoding="utf-8")
    subprocess.run([GIT, "add", "README.md"], cwd=repo, capture_output=True, check=True)
    subprocess.run([GIT, "commit", "-qm", "one"], cwd=repo, capture_output=True, check=True)

    return repo


def _fake_ssh(directory: Path) -> Path:
    """An `ssh` that runs its last argument, the remote's `git-upload-pack
    '<path>'`, on this machine. Named `ssh`, so git speaks to it as OpenSSH."""
    directory.mkdir(parents=True)
    ssh = directory / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        'for word in "$@"; do last=$word; done\n'
        f'exec /bin/sh -c "{GIT} upload-pack ${{last#git-upload-pack }}"\n',
        encoding="utf-8",
    )
    ssh.chmod(0o755)

    return ssh


# ---- which repositories one request touches --------------------------------


def test_the_named_components_map_to_their_repositories() -> None:
    assert repos_of(["chaperone", "attendance"]) == ("agent-control",)
    assert repos_of(["mcp-servers"]) == ("agent-mcp",)
    assert repos_of(["chaperone", "mcp-servers"]) == ("agent-control", "agent-mcp")


def test_a_name_the_catalog_does_not_hold_is_skipped_rather_than_guessed() -> None:
    """The request parser refuses it a moment later with root's own reason.
    Fetching for it would be this side inventing a repository name."""
    assert repos_of(["../../etc", "chaperone"]) == ("agent-control",)


# ---- the fetch itself ------------------------------------------------------


def test_a_fetch_that_works_says_which_repository_moved(tmp_path: Path) -> None:
    upstream = _make_repo(tmp_path / "upstream", "agent-control")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    subprocess.run(
        [GIT, "clone", "-q", str(upstream), str(corpus / "agent-control")],
        capture_output=True,
        check=True,
        timeout=120,
    )
    (upstream / "README.md").write_text("two\n", encoding="utf-8")
    subprocess.run([GIT, "add", "README.md"], cwd=upstream, capture_output=True, check=True)
    subprocess.run([GIT, "commit", "-qm", "two"], cwd=upstream, capture_output=True, check=True)
    head = subprocess.run(
        [GIT, "rev-parse", "HEAD"], cwd=upstream, capture_output=True, text=True, check=True
    ).stdout.strip()

    report = refresh(("agent-control",), corpus)

    assert report.ok
    assert "agent-control" in report.lines[0]
    found = subprocess.run(
        [GIT, "cat-file", "-e", f"{head}^{{commit}}"],
        cwd=corpus / "agent-control",
        capture_output=True,
    )
    assert found.returncode == 0, "the fetch did not bring the new commit"


def test_a_fetch_over_ssh_brings_a_new_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The corpus clones fetch from `git@github.com:…`, so this fetch IS an
    ssh transport. Under the executor's `core.sshCommand=false` git would
    run `false` as its ssh, every fetch would exit 128, and
    `request chaperone --wait` would poll a corpus that never moves while the
    tag sits on GitHub.

    The `ssh` here runs the remote's `git-upload-pack` locally, so this is
    real git over its ssh code path, with no network."""
    upstream = _make_repo(tmp_path / "upstream", "agent-control")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    clone = corpus / "agent-control"
    subprocess.run([GIT, "clone", "-q", str(upstream), str(clone)], capture_output=True, check=True)
    subprocess.run(
        [GIT, "remote", "set-url", "origin", f"ssh://github.test{upstream}"],
        cwd=clone,
        capture_output=True,
        check=True,
    )
    subprocess.run([GIT, "tag", "chaperone-v0.1.6"], cwd=upstream, capture_output=True, check=True)
    monkeypatch.setattr(corpus_module, "SSH", str(_fake_ssh(tmp_path / "bin")), raising=False)

    report = refresh(("agent-control",), corpus)

    assert report.ok, report.lines
    tags = subprocess.run(
        [GIT, "tag", "--list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.split()
    assert "chaperone-v0.1.6" in tags


def test_a_repository_the_corpus_does_not_hold_is_reported_not_raised(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    report = refresh(("agent-mcp",), corpus)

    assert not report.ok
    assert "agent-mcp" in report.lines[0]


def test_a_fetch_that_fails_never_stops_the_request(tmp_path: Path) -> None:
    """The corpus being behind is a release the executor refuses with a
    reason. A requester that refused to FILE would turn a network blip into
    a command the operator cannot run at all."""
    corpus = tmp_path / "corpus"
    _make_repo(corpus, "agent-control")

    report = refresh(("agent-control",), corpus)

    assert not report.ok
    assert report.lines


def test_the_fetch_runs_no_shell_and_carries_roots_own_ground(tmp_path: Path) -> None:
    """Same words the executor uses, because this is the same directory root
    will read a moment later, and the operator's own gitconfig must not decide what
    happens in it either."""
    seen: list[list[str]] = []

    def record(argv: list[str], cwd: Path, env: dict[str, str], timeout_s: float) -> int:
        del cwd, timeout_s
        seen.append(argv)
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"

        return 0

    corpus = tmp_path / "corpus"
    _make_repo(corpus, "agent-control")

    refresh(("agent-control",), corpus, run=record)

    assert len(seen) == 1
    assert seen[0][0] == GIT
    assert "core.hooksPath=/dev/null" in seen[0]
    ssh = [word for word in seen[0] if word.startswith("core.sshCommand=")]
    assert ssh[-1] == "core.sshCommand=/usr/bin/ssh", "the last -c wins, and it must be ssh"
    assert "fetch" in seen[0]
    assert all(isinstance(word, str) for word in seen[0])


def test_the_corpus_refresher_refuses_to_run_as_root(tmp_path: Path) -> None:
    """It uses the operator's credentials in the operator's own directory. Root holds no
    git credential by design (contract 06 §3.4), and root running git in a
    directory it does not own is the hazard `executor/source.py` fences."""
    corpus = tmp_path / "corpus"
    _make_repo(corpus, "agent-control")

    report = refresh(("agent-control",), corpus, euid=0)

    assert not report.ok
    assert "root" in report.lines[0]


# ---- the CLI -------------------------------------------------------------


def test_request_fetches_before_it_files_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        cli, "refresh", lambda repos, *a, **k: calls.append(repos) or CorpusReport(True, ["fine"])
    )
    spool = tmp_path / "requests"
    spool.mkdir()

    code = cli.main(
        [
            "request",
            "chaperone@0.2.0",
            "--root",
            str(_REPO_ROOT),
            "--partial",
            "--spool",
            str(spool),
        ]
    )

    assert code == 0
    assert calls == [("agent-control",)]
    captured = capsys.readouterr()
    assert "fine" in captured.out
    # The progress note goes to stderr, so `--json` stays one object.
    assert FETCH_NOTE.format(root=CORPUS_ROOT) in captured.err
    assert sorted(one.name for one in spool.iterdir()), "nothing was filed"


def test_the_json_answer_is_one_object_with_the_corpus_lines_in_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "refresh", lambda *a, **k: CorpusReport(True, ["fetched x"]))
    spool = tmp_path / "requests"
    spool.mkdir()

    code = cli.main(
        [
            "request",
            "chaperone@0.2.0",
            "--root",
            str(_REPO_ROOT),
            "--partial",
            "--json",
            "--spool",
            str(spool),
        ]
    )

    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["corpus"] == ["fetched x"]


def test_a_dry_run_fetches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dry-run` prints and writes nothing, and a `git fetch` writes."""
    calls: list[object] = []
    monkeypatch.setattr(cli, "refresh", lambda *a, **k: calls.append(a) or CorpusReport(True, []))

    code = cli.main(
        ["request", "chaperone@0.2.0", "--root", str(_REPO_ROOT), "--partial", "--dry-run"]
    )

    assert code == 0
    assert calls == []
    assert "dry run" in capsys.readouterr().out


def test_a_corpus_that_would_not_fetch_still_files_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "refresh", lambda *a, **k: CorpusReport(False, ["agent-control: no such directory"])
    )
    spool = tmp_path / "requests"
    spool.mkdir()

    code = cli.main(
        [
            "request",
            "chaperone@0.2.0",
            "--root",
            str(_REPO_ROOT),
            "--partial",
            "--spool",
            str(spool),
        ]
    )

    assert code == 0
    printed = capsys.readouterr().out + capsys.readouterr().err
    assert "no such directory" in printed


# ---- the roots are read AFTER the fetch, not before ------------------------


def _state_file(tmp_path: Path) -> str:
    """`chaperone` resolves at `latest` with no real corpus: this is a read-order
    test, not a tag-resolution one."""
    body = {"live": {}, "provided": {}, "latest": {"chaperone": "0.2.0"}, "facts": {}}
    path = tmp_path / "live-state.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    return str(path)


def _three_roots(tmp_path: Path, *, omit: str) -> tuple[Path, Path, Path]:
    """One directory per repo, the way a real request names three with
    `--root ... --root ... --root ...`. One shared root
    would not do: `mcp-servers` and `registry-data` both declare `path: "."`
    (repo root), so writing both under one directory makes the second
    overwrite the first's `component.yaml`. `omit` names the one component
    left unwritten, everywhere."""
    control = tmp_path / "agent-control"
    mcp = tmp_path / "agent-mcp"
    registry = tmp_path / "agent-registry"
    homes = {Repo.AGENT_CONTROL: control, Repo.AGENT_MCP: mcp, Repo.AGENT_REGISTRY: registry}
    for row in CATALOG:
        if row.name == omit:
            continue

        write_manifest(homes[row.repo], row.name, manifest_text(row.name))

    return control, mcp, registry


def test_the_roots_are_read_after_the_fetch_not_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that merged minutes ago reaches the corpus only through
    the fetch, so the roots are read AFTER it. Read before it,
    `request noticeboard` refuses with `no component.yaml under any root:
    mcp-servers` although the merge happened.

    This fake corpus only grows the missing manifest when "fetched", so
    the request can only succeed if the fetch really does run first."""
    control, mcp, registry = _three_roots(tmp_path, omit="mcp-servers")
    # mcp-servers' component.yaml does not exist yet: the merge "has not
    # reached" this root.

    def fake_refresh(repos: tuple[str, ...], *_a: object, **_k: object) -> CorpusReport:
        write_manifest(mcp, "mcp-servers", manifest_text("mcp-servers"))
        return CorpusReport(True, [f"fetched {name}" for name in repos])

    monkeypatch.setattr(cli, "refresh", fake_refresh)
    spool = tmp_path / "requests"
    spool.mkdir()

    code = cli.main(
        [
            "request",
            "chaperone",
            "--root",
            str(control),
            "--root",
            str(mcp),
            "--root",
            str(registry),
            "--state",
            _state_file(tmp_path),
            "--spool",
            str(spool),
        ]
    )

    assert code == cli.EXIT_OK, capsys.readouterr().err
    assert sorted(spool.glob("*.json")), "nothing was filed"


def test_a_root_still_missing_after_the_fetch_names_the_sync_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch ran and `mcp-servers` is STILL missing: the merge may
    simply not be in the corpus yet, and the refusal says the one command
    that catches it up rather than leaving the operator to guess."""
    control, mcp, registry = _three_roots(tmp_path, omit="mcp-servers")

    monkeypatch.setattr(
        cli, "refresh", lambda repos, *a, **k: CorpusReport(True, [f"fetched {n}" for n in repos])
    )
    spool = tmp_path / "requests"
    spool.mkdir()

    code = cli.main(
        [
            "request",
            "chaperone",
            "--root",
            str(control),
            "--root",
            str(mcp),
            "--root",
            str(registry),
            "--state",
            _state_file(tmp_path),
            "--spool",
            str(spool),
        ]
    )

    assert code == cli.EXIT_REFUSED
    printed = capsys.readouterr().err
    assert "mcp-servers" in printed
    assert "the merge may not be in the corpus yet" in printed
    assert "systemctl --user start code-corpus-sync.service" in printed


def test_dry_run_never_gets_the_sync_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No fetch is attempted on a dry run, so a missing root is the plain
    contract 06 §10 refusal: nothing here claims a fetch that did not run."""
    control, mcp, registry = _three_roots(tmp_path, omit="mcp-servers")

    code = cli.main(
        [
            "request",
            "chaperone",
            "--root",
            str(control),
            "--root",
            str(mcp),
            "--root",
            str(registry),
            "--state",
            _state_file(tmp_path),
            "--dry-run",
        ]
    )

    assert code == cli.EXIT_REFUSED
    printed = capsys.readouterr().err
    assert "mcp-servers" in printed
    assert "code-corpus-sync" not in printed


# ---- the PEP runs no git ---------------------------------------------------


def test_the_release_verbs_answer_says_the_corpus_is_not_its_to_refresh() -> None:
    """The verb runs as `chaperone`, which holds no git credential and owns no
    corpus. It files, and the answer names what the operator runs if the executor
    then refuses."""
    from chaperone.release_door import CORPUS_NOTE

    assert CORPUS_NOTE
    assert "sync-code-corpus" in CORPUS_NOTE


def test_the_pep_never_imports_the_corpus_refresher() -> None:
    """`handover.corpus` starts children. The PEP's process is where
    `handover/AGENTS.md` keeps them out of, and `requester/` is the whole of
    what `chaperone/` may reach in this package."""
    chaperone = Path(__file__).resolve().parents[2] / "chaperone" / "src" / "chaperone"
    for module in chaperone.rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "handover.corpus" not in text, module.name


def test_the_requester_never_imports_the_corpus_refresher() -> None:
    requester = Path(__file__).resolve().parents[1] / "src" / "handover" / "requester"
    for module in requester.rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "corpus" not in text, module.name


def test_the_corpus_package_is_the_only_second_place_that_starts_a_child() -> None:
    """`handover/AGENTS.md`: inside `executor/`, one module starts children.
    `corpus/` is the second place in the whole package, and it is one
    module, so a reader can hold both in mind."""
    package = Path(__file__).resolve().parents[1] / "src" / "handover"
    starters = sorted(
        module.relative_to(package).as_posix()
        for module in package.rglob("*.py")
        if "import subprocess" in module.read_text(encoding="utf-8")
    )

    assert starters == ["corpus/__init__.py", "executor/host.py"]


def test_the_note_the_cli_prints_names_the_corpus_root() -> None:
    """It names the root the caller passed. `--corpus` defaults to this one,
    so the line the operator reads on the host is still this path."""
    assert CORPUS_ROOT in FETCH_NOTE.format(root=CORPUS_ROOT)


_REPO_ROOT = Path(__file__).resolve().parents[2]
assert os.path.isdir(_REPO_ROOT / "handover")
