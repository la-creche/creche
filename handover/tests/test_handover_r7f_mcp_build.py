"""The per-server build (`stage7-releases.md` §4.2).

The gate row this file exists for is "a lock whose hash does not match fails
at step 8 with NOTHING swapped". Every other case here is one of
`mcpbuild`'s ten written-down assumptions, proved or refused.

No test touches the host, `uv`, `curl` or `runuser`. `McpFake` does the
smallest real thing each command does, and the tarballs are real bytes.
"""

from __future__ import annotations

import io
import re
import stat
import tarfile
from pathlib import Path

import pytest
from handover.errors import Refusal, RefusalCode
from handover.executor.host import (
    MCP_USER_RE,
    NO_MCP_USER_EXIT_CODE,
    As,
    Command,
    Host,
    make_runner,
)
from handover.executor.install import StepFailed
from handover.executor.mcpbuild import (
    HOME_DIR,
    HUP_ARGV,
    MAX_ASSET_BYTES,
    UV_CACHE_DIR_NAME,
    Fetched,
    McpBuilder,
    ServerBuild,
)
from handover.mcpserver import ServerFile, parse_server
from handover_mcp_fixtures import (
    KAGI_LOCK_PATH,
    KAGI_YAML,
    LOCK_HASH_LINE,
    LOCK_TEXT,
    VIKUNJA_YAML,
    McpFake,
    github_server,
    hostile_tarball,
    kagi_server,
    release_tarball,
    sha256_of,
    vikunja_server,
)

TAG = "mcp-servers-v0.9.4"


def _host(fake: McpFake) -> Host:
    return Host(run=fake, clock=lambda: 0.0, sleep=lambda _: None)


def _builder(
    tmp_path: Path, fake: McpFake, lock: str | None = LOCK_TEXT
) -> tuple[McpBuilder, Path, Fetched]:
    """A builder, and the registry checkout root reads its inputs from.

    `lock` is the COMMITTED closure. None writes no lock file at all, which
    is a declaration whose closure is absent.
    """
    mcp_root = tmp_path / "opt-mcp"
    mcp_root.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    tree = tmp_path / "agent-mcp"
    tree.mkdir()
    registry = tmp_path / "agent-registry"
    if lock is not None:
        committed = registry / KAGI_LOCK_PATH
        committed.parent.mkdir(parents=True)
        committed.write_text(lock, encoding="utf-8")
    else:
        registry.mkdir()

    return (
        McpBuilder(_host(fake), mcp_root),
        work,
        Fetched(registry=registry, tree=tree, tag=TAG),
    )


# -- the three sources build --------------------------------------------


def test_the_download_is_capped_at_both_ends(tmp_path: Path) -> None:
    """`--max-filesize` refuses a declared
    oversize before a byte lands; `RLIMIT_FSIZE` stops an undeclared one as
    the kernel counts the write, which is the only cap that works when the
    server sends no `Content-Length`."""
    asset = release_tarball()
    fake = McpFake(asset_bytes=asset)
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_one(github_server(sha256_of(asset)), work, source)

    fetch = next(one for one in fake.seen if one.argv[0].endswith("/curl"))
    assert "--max-filesize" in fetch.argv
    assert fetch.argv[fetch.argv.index("--max-filesize") + 1] == str(MAX_ASSET_BYTES)
    assert fetch.max_file_bytes == MAX_ASSET_BYTES


def test_only_the_download_gets_a_write_limit(tmp_path: Path) -> None:
    """A cap on a `uv` step would fail a large wheel for no reason. The
    one child that takes bytes from a name a hostile file chose is the
    one that carries it."""
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_one(kagi_server(), work, source)

    assert all(one.max_file_bytes is None for one in fake.seen)


def test_a_pypi_server_is_built_by_hash_into_its_own_new_tree(tmp_path: Path) -> None:
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    build = builder.stage_one(kagi_server(), work, source)

    assert build.paths.new.name == "kagi.new"
    assert (build.paths.new / "bin" / "kagimcp").is_file()
    assert build.artifact == "kagimcp==1.0.2"
    # The closure root generated on this host goes to the ledger.
    assert build.closure and all("--hash=sha256:" in one for one in build.closure)
    # Assumption 4: the install is hash pinned, and nothing is swapped yet.
    assert any("--require-hashes" in line for line in fake.argv_lines())
    assert not build.paths.to.exists()


def test_the_venv_is_relocatable_before_anything_fills_it(tmp_path: Path) -> None:
    """Assumption 6, for a server's own venv: without it the console
    script names `kagi.new` and exits 126 after the rename."""
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_one(kagi_server(), work, source)

    lines = fake.argv_lines()
    relocatable = next(n for n, line in enumerate(lines) if "--relocatable" in line)
    filled = next(n for n, line in enumerate(lines) if "pip install" in line)
    assert relocatable < filled


def test_a_github_release_server_is_downloaded_hashed_then_unpacked(tmp_path: Path) -> None:
    asset = release_tarball()
    fake = McpFake(asset_bytes=asset)
    builder, work, source = _builder(tmp_path, fake)

    build = builder.stage_one(github_server(sha256_of(asset)), work, source)

    assert (build.paths.new / "bin" / "github-mcp-server").is_file()
    assert build.artifact.endswith("v1.12.0/github-mcp-server_Linux_x86_64.tar.gz")
    # The proven flags: a redirect to http or file: is refused by curl.
    assert any("--proto =https" in line for line in fake.argv_lines())


def test_an_agent_mcp_server_installs_from_the_tree_this_release_fetched(tmp_path: Path) -> None:
    fake = McpFake(entrypoint="vikunja-mcp")
    builder, work, source = _builder(tmp_path, fake)

    build = builder.stage_one(vikunja_server(), work, source)

    # The artifact is the COMPONENT tag, which is what the phone showed.
    # The file names no ref at all (contract 01b §3.3).
    assert build.artifact == f"agent-mcp@{TAG}"
    # `--frozen`: the closure is that tree's own committed `uv.lock`, and
    # the install may not update it (contract 01b §3.4). `--no-editable`:
    # the distribution is built INTO the tree; an editable link would name
    # a work tree this release removes at its end.
    # `--no-dev`: the tree is the distribution alone, not its 42 dev packages.
    assert any("sync --frozen --no-editable --no-dev" in line for line in fake.argv_lines())
    assert any(one.cwd == source.tree for one in fake.seen if "sync" in one.argv)


def test_a_server_child_gets_a_home_and_a_cache_of_its_own(tmp_path: Path) -> None:
    """Without a home, a build child fails with `the staged environment
    could not be created (exit 2)`, which is `uv` saying `Failed to
    initialize cache at /nonexistent/.cache/uv`. The server user has no
    home on purpose (`host.NO_HOME`), so the build gives the child a
    cache directory it can create. The rehearsal's fake `uv` needs no
    cache, so only this test can see it."""
    for server, fake in (
        (kagi_server(), McpFake()),
        (vikunja_server(), McpFake(entrypoint="vikunja-mcp")),
    ):
        sub = tmp_path / server.name
        sub.mkdir()
        builder, work, source = _builder(sub, fake)

        builder.stage_one(server, work, source)

        home = work / server.name / HOME_DIR
        children = [one for one in fake.seen if one.identity is As.MCP]
        assert len(children) == 2, server.name  # the venv, then the install
        for child in children:
            env = dict(child.env)
            assert env["HOME"] == str(home)
            assert env["UV_CACHE_DIR"] == str(home / UV_CACHE_DIR_NAME)
            assert env["UV_PYTHON_DOWNLOADS"] == "never"
        # Root made the home and handed it to the server's user before any
        # child ran, and the home is inside the per-request work tree, so
        # root's cleanup at the end takes the cache with it.
        made = next(
            one
            for one in fake.seen
            if one.argv[0].endswith("/install") and one.argv[-1] == str(home)
        )
        assert made.argv[made.argv.index("-o") + 1] == server.user
        assert fake.seen.index(made) < fake.seen.index(children[0])


def test_the_work_root_is_opened_down_to_the_staging_directory(tmp_path: Path) -> None:
    """The child's home is under `<work root>/<request>/mcp/<name>/home`,
    and the release unit's `UMask=0027` makes `/srv/agents/work/release`
    and the request's own directory `0750 root:root`, which `stage_one`
    does not open. Every directory from the work root down is `0755`
    before any child runs."""
    fake = McpFake()
    builder, _, source = _builder(tmp_path, fake)
    root = tmp_path / "work-root"
    root.mkdir(mode=0o700)
    request_dir = root / "01REQUEST"
    request_dir.mkdir(mode=0o700)
    work = request_dir / "mcp"
    work.mkdir(mode=0o700)

    builder.stage_all((kagi_server(),), work, source, traversable_from=root)

    for directory in (root, request_dir, work, work / kagi_server().name):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755, directory
    # The root's OTHER children are not touched: only the chain to `work`.
    other = root / "01OTHER"
    other.mkdir(mode=0o700)
    builder.stage_all((kagi_server(),), work, source, traversable_from=root)
    assert stat.S_IMODE(other.stat().st_mode) == 0o700


def test_a_failed_child_says_what_it_printed(tmp_path: Path) -> None:
    """Two releases failed with `the staged environment could not be
    created (exit 2)` and nothing else; each cause had to be found by
    running the child again by hand. The last stderr lines ride along."""
    fake = McpFake(fails={"venv": 2})
    builder, work, source = _builder(tmp_path, fake)

    with pytest.raises(StepFailed) as failed:
        builder.stage_one(kagi_server(), work, source)

    assert "the staged environment could not be created (exit 2): venv failed" in str(failed.value)


def ha_like_server() -> ServerFile:
    """A pypi server that wants a minor the host does not have. `ha-mcp`
    requires `>=3.13,<3.15` and the host has 3.12."""
    body = KAGI_YAML.format(lock=KAGI_LOCK_PATH).replace('python: "3.12"', 'python: "3.13"')

    return parse_server(body.encode("utf-8"), "kagi")


def test_a_minor_the_host_lacks_is_installed_by_root_once(tmp_path: Path) -> None:
    """A minor the host lacks fails the build with `No interpreter found
    for Python 3.13 in managed installations or search path`. Root asks
    first, installs only what
    nobody provides, into the shared directory under the MCP root, opens
    it, and the next release finds it there."""
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_all((ha_like_server(),), work, source)

    lines = fake.argv_lines()
    asked = next(n for n, line in enumerate(lines) if "python find 3.13" in line)
    installed = next(n for n, line in enumerate(lines) if "python install --no-bin 3.13" in line)
    venv = next(n for n, line in enumerate(lines) if "venv" in line)
    assert asked < installed < venv
    root_children = [one for one in fake.seen if "python" in one.argv[1:2]]
    for child in root_children:
        assert child.identity is As.ROOT
        assert dict(child.env)["UV_PYTHON_INSTALL_DIR"] == str(builder.python_root)
    # Asking never downloads; installing is the one child that may.
    assert dict(fake.seen[asked].env)["UV_PYTHON_DOWNLOADS"] == "never"
    assert "UV_PYTHON_DOWNLOADS" not in dict(fake.seen[installed].env)
    # Opened for every `mcp-<name>`: root's umask had made it 0750.
    for directory in builder.python_root.rglob("*"):
        if directory.is_dir():
            assert stat.S_IMODE(directory.stat().st_mode) == 0o755, directory

    again = McpFake()
    (tmp_path / "again").mkdir()
    builder, work, source = _builder(tmp_path / "again", again)
    builder.python_root = tmp_path / "opt-mcp" / ".python"
    builder.stage_all((ha_like_server(),), work, source)

    assert not again.ran("python install")


def test_a_minor_the_host_provides_is_not_installed(tmp_path: Path) -> None:
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_all((kagi_server(),), work, source)

    assert fake.ran("python find 3.12")
    assert not fake.ran("python install")


def test_every_server_child_sees_the_shared_interpreters(tmp_path: Path) -> None:
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_one(kagi_server(), work, source)

    for child in (one for one in fake.seen if one.identity is As.MCP):
        env = dict(child.env)
        assert env["UV_PYTHON_INSTALL_DIR"] == str(builder.python_root)
        assert env["UV_PYTHON_DOWNLOADS"] == "never"


def test_a_server_tree_that_reads_code_outside_itself_is_refused(tmp_path: Path) -> None:
    """Contract 06 §8.2 for a server tree. The first `mcp-servers` release
    on the host was refused because the COMPONENT tree held
    `_editable_impl_agent_mcp.pth`; every server tree was about to get the
    same file, naming root's work tree, and nothing checked them."""
    elsewhere = tmp_path / "work" / "01ELSEWHERE" / "agent-mcp" / "src"
    fake = McpFake(entrypoint="vikunja-mcp", pth_outside=str(elsewhere))
    builder, work, source = _builder(tmp_path, fake)

    with pytest.raises(Refusal) as refused:
        builder.stage_one(vikunja_server(), work, source)

    assert refused.value.code is RefusalCode.EDITABLE
    assert refused.value.subject == vikunja_server().name
    assert "not self-contained" in refused.value.detail
    # Nothing swapped: the live path never appeared.
    assert not builder.paths_of(vikunja_server().name).to.exists()


def test_a_pypi_server_tree_is_checked_the_same_way(tmp_path: Path) -> None:
    fake = McpFake(pth_outside="/somewhere/else")
    builder, work, source = _builder(tmp_path, fake)

    with pytest.raises(Refusal) as refused:
        builder.stage_one(kagi_server(), work, source)

    assert refused.value.code is RefusalCode.EDITABLE


# -- the gate row: a hash that does not match ---------------------------


def test_an_asset_whose_hash_differs_fails_with_nothing_swapped(tmp_path: Path) -> None:
    """The §3 gate row. The refusal names the artifact and BOTH hashes, the
    tree is never unpacked, and `/opt/mcp/<name>` does not exist."""
    asset = release_tarball()
    fake = McpFake(asset_bytes=asset)
    builder, work, source = _builder(tmp_path, fake)
    declared = "0" * 64

    with pytest.raises(Refusal) as caught:
        builder.stage_one(github_server(declared), work, source)

    assert caught.value.code is RefusalCode.SERVER
    assert "github-mcp-server_Linux_x86_64.tar.gz" in caught.value.detail
    assert sha256_of(asset) in caught.value.detail
    assert declared in caught.value.detail
    assert not (builder.mcp_root / "github-code").exists()


def test_the_build_resolves_nothing(tmp_path: Path) -> None:
    """A closure the build produces cannot pin anything: `--require-hashes`
    then checks the hashes root just wrote against themselves. The lock is
    an INPUT, committed beside `server.yaml` (contract 01b §3.4), so no
    resolution may run during a release.
    """
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_one(kagi_server(), work, source)

    assert not any("compile" in line for line in fake.argv_lines())
    assert not any("--generate-hashes" in line for line in fake.argv_lines())


def test_the_install_reads_the_committed_lock(tmp_path: Path) -> None:
    """The hashes root installs against come from the file in the registry.

    Root copies that file into the staging directory first, so the
    unprivileged child reads bytes root has already validated rather than
    reaching into the checkout itself. The copy must be the committed file
    and not a rewrite of it, which is what the byte comparison says.
    """
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    build = builder.stage_one(kagi_server(), work, source)

    installed_from = next(
        Path(one.argv[one.argv.index("--requirement") + 1])
        for one in fake.seen
        if "--requirement" in one.argv
    )
    committed = source.registry / KAGI_LOCK_PATH
    assert installed_from.read_text(encoding="utf-8") == committed.read_text(encoding="utf-8")
    assert build.closure == (LOCK_HASH_LINE,)


def test_a_committed_lock_that_does_not_pin_the_declaration_is_refused(
    tmp_path: Path,
) -> None:
    """Contract 01b §3.4 rule 5. The two came from one pull request, so
    disagreeing means one of them was edited alone."""
    fake = McpFake()
    builder, work, source = _builder(
        tmp_path, fake, lock="kagimcp==9.9.9 \\\n    --hash=sha256:" + "ab" * 32 + "\n"
    )

    with pytest.raises(Refusal, match="does not pin"):
        builder.stage_one(kagi_server(), work, source)

    assert not (builder.mcp_root / "kagi").exists()


def test_a_committed_lock_with_no_hashes_is_refused(tmp_path: Path) -> None:
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake, lock="kagimcp==1.0.2\n")

    with pytest.raises(Refusal, match="carries no hashes"):
        builder.stage_one(kagi_server(), work, source)


def test_a_missing_lock_is_refused(tmp_path: Path) -> None:
    """Nothing is installed unpinned. A declaration whose closure is absent
    is a release that would resolve."""
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake, lock=None)

    with pytest.raises(Refusal, match="closure"):
        builder.stage_one(kagi_server(), work, source)


@pytest.mark.parametrize(
    "path",
    ["../../../etc/passwd", "/etc/passwd", "mcp/kagi/../../../etc/passwd"],
)
def test_a_lock_path_that_leaves_the_registry_is_refused(path: str) -> None:
    """`lock` is a repo-relative path out of a hostile file, so it gets the
    same treatment every other path does."""
    with pytest.raises(Refusal):
        kagi_server(lock=path)


def test_a_symlinked_lock_is_not_followed(tmp_path: Path) -> None:
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake, lock=None)
    elsewhere = tmp_path / "elsewhere.lock"
    elsewhere.write_text(LOCK_TEXT, encoding="utf-8")
    target = source.registry / "mcp" / "kagi" / "install.lock"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(elsewhere)

    with pytest.raises(Refusal, match="closure"):
        builder.stage_one(kagi_server(), work, source)


def test_an_install_that_refuses_the_hashes_stops_the_stage(tmp_path: Path) -> None:
    """`uv pip install --require-hashes` exiting non-zero is the other half
    of the same row: an artifact whose hash moved under the lock."""
    fake = McpFake(fails={"--require-hashes": 2})
    builder, work, source = _builder(tmp_path, fake)

    with pytest.raises(StepFailed, match="would not install"):
        builder.stage_one(kagi_server(), work, source)

    assert not (builder.mcp_root / "kagi").exists()


def test_an_agent_mcp_server_that_declares_a_ref_is_refused() -> None:
    """Assumption 9. A ref would be a second version beside the component
    tag, in another tag namespace, and the two never meet. There is no
    second version to disagree with, so a file carrying one is refused
    where its other pin fields are."""
    with pytest.raises(Refusal, match=re.escape("install.ref does not belong to agent-mcp")):
        parse_server(
            VIKUNJA_YAML.replace("source: agent-mcp", "source: agent-mcp\n  ref: v0.4.6").encode(
                "utf-8"
            ),
            "vikunja-finance",
        )


# -- assumption 3: nothing third-party runs as root ---------------------


def test_every_package_command_runs_as_the_server_s_own_user(tmp_path: Path) -> None:
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_one(kagi_server(), work, source)

    for command in fake.seen:
        if command.argv[0].endswith("/uv"):
            assert command.identity is As.MCP
            assert command.user == "mcp-kagi"


def test_the_tree_is_handed_back_to_root_after_the_build(tmp_path: Path) -> None:
    """§4.2 step 4. Its reason is assumption 3: the tree is the server
    user's while it is built, so it must stop being so before it is live.

    The OWNER still needs a child, because only `chown` moves a tree
    between users. The modes are not a child: a `chmod -R a+rX` child is
    one this fake answers 0 to without touching a byte, so the mode that
    decides whether the PEP can start the server is
    `test_handover_mcm_tree_modes.py`'s to test."""
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_one(kagi_server(), work, source)

    lines = fake.argv_lines()
    assert any("chown -R root:root" in line for line in lines)
    assert not any("chmod" in line for line in lines)


def test_a_command_that_cannot_name_its_user_does_not_run_as_root() -> None:
    """`host.make_runner`'s fail-closed end. Nothing starts a real child:
    the guard answers before `subprocess` is reached."""
    run = make_runner(operator_uid=1000)

    for user in (None, "root", "../root", "mcp-kagi\nroot"):
        result = run(Command(("/bin/true",), As.MCP, 1.0, user=user))
        assert result.code == NO_MCP_USER_EXIT_CODE


def test_the_user_pattern_accepts_only_a_server_s_own_name() -> None:
    assert MCP_USER_RE.fullmatch("mcp-github-code")
    assert not MCP_USER_RE.fullmatch("mcp-")
    assert not MCP_USER_RE.fullmatch("root")


# -- assumption 5: the unpack refuses five shapes ------------------------


def _unpack(tmp_path: Path, raw: bytes) -> ServerBuild:
    fake = McpFake(asset_bytes=raw)
    builder, work, source = _builder(tmp_path, fake)

    return builder.stage_one(github_server(sha256_of(raw)), work, source)


def _member(tar: tarfile.TarFile, name: str, mode: int = 0o755) -> None:
    body = b"x"
    info = tarfile.TarInfo(name)
    info.size = len(body)
    info.mode = mode
    tar.addfile(info, io.BytesIO(body))


@pytest.mark.parametrize("name", ["/etc/cron.d/x", "../../etc/cron.d/x", "a/../../../x"])
def test_a_member_that_leaves_its_tree_is_refused(tmp_path: Path, name: str) -> None:
    raw = hostile_tarball(lambda tar: _member(tar, name))

    with pytest.raises(Refusal, match="leaves its tree"):
        _unpack(tmp_path, raw)


def test_a_symlink_member_is_refused(tmp_path: Path) -> None:
    def make(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("github-mcp-server")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/shadow"
        tar.addfile(info)

    with pytest.raises(Refusal, match="holds a link"):
        _unpack(tmp_path, hostile_tarball(make))


def test_a_device_member_is_refused(tmp_path: Path) -> None:
    def make(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("dev/sda")
        info.type = tarfile.BLKTYPE
        tar.addfile(info)

    with pytest.raises(Refusal, match="not a file or a directory"):
        _unpack(tmp_path, hostile_tarball(make))


def test_a_setuid_member_is_refused(tmp_path: Path) -> None:
    raw = hostile_tarball(lambda tar: _member(tar, "github-mcp-server", mode=0o4755))

    with pytest.raises(Refusal, match="setuid or setgid"):
        _unpack(tmp_path, raw)


def test_an_asset_without_the_declared_entrypoint_is_refused(tmp_path: Path) -> None:
    raw = release_tarball(entrypoint="something-else")

    with pytest.raises(Refusal, match="holds no github-mcp-server"):
        _unpack(tmp_path, raw)


def test_an_unpacked_file_keeps_no_more_than_0755(tmp_path: Path) -> None:
    raw = hostile_tarball(lambda tar: _member(tar, "github-mcp-server", mode=0o777))

    build = _unpack(tmp_path, raw)

    assert build.paths.new.joinpath("bin", "github-mcp-server").stat().st_mode & 0o777 == 0o755


# -- assumption 6's failure mode, and the entrypoint check ---------------


def test_an_entrypoint_that_still_names_the_staging_tree_is_refused(tmp_path: Path) -> None:
    """Assumption 6's failure mode, caught before the switch. The shebang
    is written with the staged path `uv` would have put in it."""
    staged = tmp_path / "opt-mcp" / "kagi.new"
    fake = McpFake(shebang=f"#!{staged}/bin/python\n")
    builder, work, source = _builder(tmp_path, fake)

    with pytest.raises(Refusal, match="still names the staging tree"):
        builder.stage_one(kagi_server(), work, source)


def test_a_wrapper_script_that_names_the_staging_tree_on_line_two_is_refused(
    tmp_path: Path,
) -> None:
    """The case a one-line read would miss. `uv` writes this form when the
    staged path is too long for a `#!` line, and a real `uv` produced it in
    `test_handover_r7f_server_venv.py`."""
    staged = tmp_path / "opt-mcp" / "kagi.new"
    wrapper = f"#!/bin/sh\n'''exec' '{staged}/bin/python' \"$0\" \"$@\"\n' '''\n"
    fake = McpFake(shebang=wrapper)
    builder, work, source = _builder(tmp_path, fake)

    with pytest.raises(Refusal, match="still names the staging tree"):
        builder.stage_one(kagi_server(), work, source)


def test_a_build_that_leaves_no_entrypoint_is_refused(tmp_path: Path) -> None:
    fake = McpFake(entrypoint="not-the-one")
    builder, work, source = _builder(tmp_path, fake)

    with pytest.raises(Refusal, match="holds no entrypoint"):
        builder.stage_one(kagi_server(), work, source)


# -- assumption 10: stage everything, then swap --------------------------


def test_staging_two_servers_swaps_neither(tmp_path: Path) -> None:
    asset = release_tarball()
    fake = McpFake(asset_bytes=asset)
    builder, work, source = _builder(tmp_path, fake)

    builds = builder.stage_all((github_server(sha256_of(asset)),), work, source)

    assert all(not one.paths.to.exists() for one in builds)
    assert all(one.paths.new.is_dir() for one in builds)


def test_a_swap_keeps_the_previous_tree_and_a_swap_back_restores_it(tmp_path: Path) -> None:
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)
    live = builder.mcp_root / "kagi"
    live.mkdir()
    (live / "marker").write_text("the version that was serving\n", encoding="utf-8")

    build = builder.stage_one(kagi_server(), work, source)
    builder.swap_in(build)

    assert (build.paths.prev / "marker").is_file()
    assert (live / "bin" / "kagimcp").is_file()

    builder.swap_back(build)

    assert (live / "marker").is_file()


def test_the_last_action_is_one_signal_to_the_pep_unit(tmp_path: Path) -> None:
    """§4.4 step 1. An argv list, to the unit by name, as root."""
    fake = McpFake()
    builder, _, _ = _builder(tmp_path, fake)

    builder.reload_chaperone()

    assert fake.seen[-1].argv == HUP_ARGV
    assert fake.seen[-1].identity is As.ROOT


# -- assumption 2: the name is the only thing that becomes a path --------


def test_a_path_that_leaves_the_mcp_root_is_refused(tmp_path: Path) -> None:
    fake = McpFake()
    builder, _, _ = _builder(tmp_path, fake)

    with pytest.raises(Refusal, match="outside the MCP root"):
        builder.paths_of("../etc/cron.d")


# -- assumption 7: the build never sees a secret -------------------------


#: The only names a build child's environment may carry: where `uv` may
#: write, and where it may not fetch from. Nothing a `server.yaml` names.
BUILD_ENV_NAMES = frozenset(
    {
        "HOME",
        "UV_CACHE_DIR",
        "UV_PYTHON_DOWNLOADS",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PROJECT_ENVIRONMENT",
    }
)


def test_no_child_of_the_build_carries_an_environment_value(tmp_path: Path) -> None:
    """`run.env` is not read here at all. A server's credential reaches it
    from the PEP at spawn time (invariant 13). The build's own variables
    (`HOME_DIR`) say where `uv` may write, and carry no value of the
    server's."""
    fake = McpFake()
    builder, work, source = _builder(tmp_path, fake)

    builder.stage_one(kagi_server(), work, source)

    for command in fake.seen:
        assert {name for name, _ in command.env} <= BUILD_ENV_NAMES
        assert not any("KAGI" in word for word in command.argv)
        assert not any("KAGI" in value for _, value in command.env)
