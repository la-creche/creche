"""What `bin/creche-deploy` can be held to without the host.

The deploy is the single sudoers entry on the host and it runs as root in a
tree every service reads, so the parts worth pinning are the ones that
REMOVE something. There is one: the `git clean` that takes the leftovers of
the directories this tree no longer has.

Three things about that command are easy to get wrong and expensive to get
wrong on the host, so each has a test here:

1. **`-X`, never `-x`.** `-X` removes only what `.gitignore` already covers.
   `-x` would take every untracked file, and in `/opt/creche` that
   includes whatever root or the operator put there by hand.
2. **The workspace venv survives.** Every unit runs out of
   `/opt/creche/.venv`, and `uv sync` fills it two steps later in the
   same script.
3. **`-e` alone does not spare a path under `-X`.** It ADDS a pattern to the
   ignore rules, so `-e .venv` marks the venv for removal rather than keeping
   it. `test_an_exclude_pattern_would_not_have_kept_the_venv` is that
   measurement, written down, because reading the flag's name is what makes
   the mistake.

The deploy also brings a SECOND checkout up to date: the site, this
deployment's own repository, beside the product. `deploy_site` is held to
what a first run, a later run and a host with no site each do.

Everything here runs against a throwaway repository this file builds with the
real `git`. Nothing reaches a live service and nothing needs the host.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "bin" / "creche-deploy"

#: The venv every unit on the host runs from, relative to the deployed tree.
VENV = ".venv"

#: A throwaway repository shaped like `/opt/creche`: three deleted
#: component directories that still hold ignored leftovers, one live
#: directory that holds both tracked files and leftovers, the workspace venv,
#: and one untracked file that is not ignored at all.
IGNORES = ".venv/\n__pycache__/\n*.egg-info/\nnode_modules/\n"
FIXTURE = {
    ".gitignore": IGNORES,
    "pyproject.toml": "[project]\n",
    # Deleted from the tree: nothing but leftovers is left on the host.
    "materializer/__pycache__/host.pyc": "pyc",
    "ui/node_modules/react/index.js": "js",
    "shim/src/agent_shim.egg-info/PKG-INFO": "egg",
    # Live: tracked source beside leftovers of its own.
    "chaperone/src/chaperone/host.py": "py",
    "chaperone/src/chaperone/__pycache__/host.pyc": "pyc",
    # The venv the units run from.
    f"{VENV}/bin/chaperone": "#!/bin/sh\n",
    f"{VENV}/lib/python3.12/site-packages/chaperone/__init__.py": "",
    # Untracked and NOT ignored: `-X` must leave it, `-x` would not.
    "left-by-hand.txt": "a decrypted secret, or a copy of a unit",
}
TRACKED = (".gitignore", "pyproject.toml", "chaperone/src/chaperone/host.py")

#: What the leftovers' own directories are called, once the files under them
#: are gone: `git clean -d` removes the directory too.
GONE_DIRS = ("materializer", "ui", "shim/src/agent_shim.egg-info")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120
    )


@pytest.fixture
def deployed(tmp_path: Path) -> Path:
    """The fake `/opt/creche`, committed, with its leftovers in place."""
    repo = tmp_path / "opt" / "agent-control"
    repo.mkdir(parents=True)
    assert _git(repo, "init", "-q").returncode == 0
    for name, body in FIXTURE.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        # An EXPLICIT mode, never the umask's: a fixture that depends on the
        # developer's umask passes here and fails on the CI runner.
        path.chmod(0o644)

    assert _git(repo, "add", *TRACKED).returncode == 0
    made = _git(
        repo, "-c", "user.email=deploy@test", "-c", "user.name=deploy", "commit", "-qm", "deployed"
    )
    assert made.returncode == 0, made.stdout + made.stderr

    return repo


def _clean_argv() -> list[str]:
    """The script's own `git clean` line, as argv.

    Parsed rather than restated, so the command the tests below RUN is the
    command root runs. The `||` guard that keeps a failed tidy-up from ending
    the deploy is cut off here, and so is the backslash that carries it onto
    the next line — `shlex` reads that one as an escape and raises.
    """
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().removesuffix("\\").strip()
        if stripped.startswith("git clean"):
            return shlex.split(stripped.split("||")[0])

    raise AssertionError(f"no `git clean` line in {SCRIPT}")


def test_the_clean_is_exactly_this_argv() -> None:
    """Pinned character for character. Every other test here runs what this
    one reads, so a changed flag is a failure with a reason rather than a
    quietly different measurement."""
    assert _clean_argv() == ["git", "clean", "-fdX", "-e", "!/.venv/"]


def test_the_clean_removes_a_deleted_directorys_leftovers(deployed: Path) -> None:
    done = _git(deployed, *_clean_argv()[1:])

    assert done.returncode == 0, done.stdout + done.stderr
    assert not (deployed / "materializer" / "__pycache__" / "host.pyc").exists()
    assert not (deployed / "ui" / "node_modules" / "react" / "index.js").exists()
    for name in GONE_DIRS:
        assert not (deployed / name).exists(), name


def test_the_clean_keeps_the_venv_every_unit_runs_from(deployed: Path) -> None:
    """`/opt/creche/.venv` is what `creche-chaperone.service` execs from and
    what `uv sync` fills two steps later in the same script."""
    done = _git(deployed, *_clean_argv()[1:])

    assert done.returncode == 0, done.stdout + done.stderr
    assert (deployed / VENV / "bin" / "chaperone").is_file()
    assert (deployed / VENV / "lib/python3.12/site-packages/chaperone/__init__.py").is_file()


def test_the_clean_keeps_what_git_does_not_ignore(deployed: Path) -> None:
    """The whole reason for `-X`. An untracked file that no `.gitignore`
    covers is something a person put there, and root cannot tell a stale
    cache from a decrypted secret."""
    assert _git(deployed, *_clean_argv()[1:]).returncode == 0

    assert (deployed / "left-by-hand.txt").is_file()


def test_a_live_directory_keeps_its_tracked_files(deployed: Path) -> None:
    """`chaperone/` is not deleted: it loses its `__pycache__` and keeps its
    source. The next two steps of the deploy write that cache again."""
    assert _git(deployed, *_clean_argv()[1:]).returncode == 0

    assert (deployed / "chaperone/src/chaperone/host.py").is_file()
    assert not (deployed / "chaperone/src/chaperone/__pycache__").exists()


def test_an_exclude_pattern_would_not_have_kept_the_venv(deployed: Path) -> None:
    """The measurement the script's comment rests on (git 2.54).

    `-e <pattern>` adds the pattern to the ignore rules IN EFFECT, and `-X`
    removes what those rules cover — so the obvious spelling marks the venv
    for removal instead of sparing it. The leading `!` is what un-ignores.
    """
    done = _git(deployed, "clean", "-ndX", "-e", ".venv")

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"{VENV}/" in done.stdout, done.stdout


def test_the_keep_is_anchored_to_the_top_of_the_tree(deployed: Path) -> None:
    """A component build leaves its own venv deeper in the tree. Only the one
    the units run from is kept, which is what the leading `/` buys."""
    nested = deployed / "toybox" / VENV / "bin"
    nested.mkdir(parents=True)
    (nested / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    assert _git(deployed, *_clean_argv()[1:]).returncode == 0

    assert not (deployed / "toybox").exists()
    assert (deployed / VENV / "bin" / "chaperone").is_file()


def test_the_deploy_never_cleans_everything_untracked() -> None:
    """`-x` in this script would be a root command that removes files nobody
    ignored. The flag is one character from the one that is there."""
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "git clean" not in stripped:
            continue

        assert "-x" not in stripped, stripped


def test_the_clean_runs_after_the_checkout_and_before_the_build() -> None:
    """Order, and both halves matter. Before the checkout it would clean the
    previous ref's tree; after `uv sync --compile-bytecode` and `compileall`
    it would delete the caches the deploy had just written."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    checkout = next(i for i, one in enumerate(lines) if one.startswith("git checkout -f"))
    clean = next(i for i, one in enumerate(lines) if one.startswith("git clean"))
    sync = next(i for i, one in enumerate(lines) if "uv sync --frozen" in one)

    assert checkout < clean < sync, (checkout, clean, sync)


def test_a_failed_clean_does_not_end_the_deploy() -> None:
    """`set -euo pipefail` is the first line of this script. Housekeeping
    that can stop a deploy is worse than an untidy tree."""
    body = SCRIPT.read_text(encoding="utf-8")
    after = body.split("git clean", 1)[1].split("\n\n", 1)[0]

    assert "||" in after, after
    assert "WARNING" in after, after


# ---- the old MCP install ----------------------------------------------------

#: The parent the deploy built three MCP trees under: the
#: agent-mcp + kagimcp venv, the ha-mcp venv and the github-mcp-server
#: binary. An `mcp-servers` release builds every MCP server now, into
#: `/opt/mcp/<name>` (`docs/rework/stage7-releases.md` §4.2).
OLD_MCP_PARENT = "/opt/agent-pep-mcp"

#: What each piece of that install reached for. None of it may come back.
OLD_INSTALL_WORDS = (
    OLD_MCP_PARENT,
    "mcp-pins.env",
    "kagimcp.lock",
    "ha-mcp.lock",
    "github-mcp-server",
    "uv venv",
    "uv pip",
    "uv python",
    "curl ",
)

#: The files that existed only to feed that install.
OLD_INSTALL_INPUTS = (
    "chaperone/mcp-pins.env",
    "chaperone/kagimcp.lock",
    "chaperone/kagimcp-requirements.in",
    "chaperone/ha-mcp.lock",
)


def _code_lines() -> list[str]:
    """Every line of the script that is not a comment."""
    lines = (one.strip() for one in SCRIPT.read_text(encoding="utf-8").splitlines())

    return [one for one in lines if one and not one.startswith("#")]


def test_the_deploy_installs_no_mcp_server() -> None:
    """A second installer would build trees no release approved, on a path
    the roster root writes never names. The one installer is the
    `mcp-servers` release."""
    for line in _code_lines():
        for word in OLD_INSTALL_WORDS:
            assert word not in line, line


def test_the_deploy_leaves_the_old_trees_to_the_verb_that_removes_them() -> None:
    """A deploy is not the verb that removes something. The old trees stay
    on disk, and the script says which verb takes them away, so a reader of
    the host does not take them for something the deploy forgot."""
    text = SCRIPT.read_text(encoding="utf-8")

    assert "rework-retire-old.sh remove-old-trees" in text


@pytest.mark.parametrize("name", OLD_INSTALL_INPUTS)
def test_the_old_install_inputs_are_gone(name: str) -> None:
    assert not (REPO_ROOT / name).exists(), name


#: `deploy_site` and the one constant it reads, cut out of the script. The
#: function is run as root runs it, minus root: everything around it in the
#: script needs a host.
SITE_FUNCTION = re.compile(r"^deploy_site\(\) \{\n.*?^\}\n", re.MULTILINE | re.DOTALL)
SITE_REF_LINE = re.compile(r"^SITE_REF=.*$", re.MULTILINE)


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")
    assert _git(repo, "add", name).returncode == 0
    made = _git(repo, "-c", "user.email=site@test", "-c", "user.name=site", "commit", "-qm", name)
    assert made.returncode == 0, made.stdout + made.stderr


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A host: the operator's two checkouts under `code/`, and root's clone
    of the product under `opt/` with its origin where bootstrap leaves it."""
    for name in ("agent-control", "private-docs"):
        repo = tmp_path / "code" / name
        repo.mkdir(parents=True)
        assert _git(repo, "init", "-q", "-b", "main").returncode == 0
        _commit(repo, "README.md", name)

    product = tmp_path / "opt" / "agent-control"
    cloned = subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "code" / "agent-control"), str(product)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert cloned.returncode == 0, cloned.stderr

    return tmp_path


def _deploy_site(host: Path) -> subprocess.CompletedProcess[str]:
    """The script's own `deploy_site`, run against the fixture host."""
    text = SCRIPT.read_text(encoding="utf-8")
    function = SITE_FUNCTION.search(text)
    ref = SITE_REF_LINE.search(text)
    assert function is not None and ref is not None, f"no deploy_site in {SCRIPT}"
    program = f'set -euo pipefail\n{ref.group(0)}\n{function.group(0)}\ndeploy_site "$1" "$2"\n'

    return subprocess.run(
        [
            "bash",
            "-c",
            program,
            "deploy",
            str(host / "opt/agent-control"),
            str(host / "opt/private-docs"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_first_run_clones_the_site_beside_the_product(host: Path) -> None:
    """Root needs no step of its own: the checkout beside the product's
    origin is the site's origin, and the first deploy clones it."""
    done = _deploy_site(host)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (host / "opt/private-docs/README.md").read_text(encoding="utf-8") == "private-docs"
    assert done.stdout.startswith("site ")


def test_a_later_run_follows_the_sites_origin(host: Path) -> None:
    assert _deploy_site(host).returncode == 0
    _commit(host / "code/private-docs", "site.env", "LAN_HOST=192.0.2.10\n")

    done = _deploy_site(host)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (host / "opt/private-docs/site.env").is_file()


def test_a_later_run_discards_what_was_changed_in_the_deployed_site(host: Path) -> None:
    """The deployed site is a copy root reads, never a place to edit: the
    operator's checkout is. `checkout -f` puts a changed file back."""
    assert _deploy_site(host).returncode == 0
    (host / "opt/private-docs/README.md").write_text("edited on the host", encoding="utf-8")

    assert _deploy_site(host).returncode == 0

    assert (host / "opt/private-docs/README.md").read_text(encoding="utf-8") == "private-docs"


def test_a_host_with_no_site_checkout_deploys_the_product_alone(host: Path) -> None:
    shutil.rmtree(host / "code/private-docs")

    done = _deploy_site(host)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "the product deploys alone" in done.stdout
    assert not (host / "opt/private-docs").exists()


def test_a_site_that_cannot_be_fetched_says_so(host: Path) -> None:
    """The caller decides what a stale site costs, so the function must
    report it rather than print a deployed line over an old tree."""
    assert _deploy_site(host).returncode == 0
    shutil.rmtree(host / "code/private-docs")

    done = _deploy_site(host)

    assert done.returncode != 0
    assert "site " not in done.stdout
