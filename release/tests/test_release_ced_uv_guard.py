"""The one test that builds a real component tree with the real `uv`.

The host shipped an editable tree through FOUR gates. Every one of them put
a fake `uv` on the `PATH`, so nothing ever ran the argv a manifest actually
carries, and the fault — `_editable_impl_agent_managerd.pth` naming
`/opt/agent-control/managerd/src` — reached production and stayed there.

So this module runs the manifest's OWN build argv, with the `uv` this
machine has, and then asks the built tree two questions:

1. Does any `.pth` in it name a path outside it?
2. With the source directory GONE, does the package still import, and does
   it import from inside the tree?

Question 2 is the one a fake cannot answer. The workspace is copied into
`tmp_path` first and the COPY's `src` is renamed, because `pre-push` runs
`pytest -n auto` and renaming a directory in the shared checkout would
break whatever another worker is reading.

`releasectl` is the smallest `kind: venv` component — one dependency, a
1.3 MB tree, about a second — which is why the guard builds that one.

Marked slow: it copies the workspace and builds an environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Final

import pytest
from agent_release.executor.install import RELOCATABLE_ARGV, UV, VENV_ENV_NAME
from agent_release.executor.selfcontained import SITE_GLOB, escapes
from agent_release.manifest import parse_manifest

pytestmark = pytest.mark.slow

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: The smallest `kind: venv` component, and its manifest.
COMPONENT: Final = "releasectl"
COMPONENT_DIR: Final = "release"
PACKAGE: Final = "agent_release"

BUILD_TIMEOUT_S: Final = 600.0

#: What the copy leaves behind. `.venv` is 112 MB of the 130 MB this
#: worktree holds, and the build makes its own environment anyway.
SKIP_COPY: Final = shutil.ignore_patterns(
    ".venv",
    ".venv-toybox",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".pnpm-store",
)

#: A build failure that means this machine could not reach the index, and
#: not that the repository is broken. Narrow on purpose: "no solution
#: found" and a missing lock entry are real failures and must not skip.
NETWORK_MARKERS: Final = (
    "failed to fetch",
    "could not connect",
    "dns error",
    "name resolution",
    "offline",
    "network",
    "timed out",
)


def _uv() -> str:
    """The `uv` THIS machine has. Production names `/usr/local/bin/uv`,
    because a child's `PATH` is built by `host.child_env`."""
    found = shutil.which("uv")

    return found if found is not None else UV


@pytest.fixture
def uv_available() -> None:
    if shutil.which("uv") is None and not Path(UV).exists():
        pytest.skip("uv is on neither the PATH nor /usr/local/bin: the guard cannot build")


def _build_argv() -> list[str]:
    """The manifest's own `build`, with this machine's `uv` in front.

    Read from `component.yaml` and never restated here: a manifest that
    lost `--no-editable` must fail this guard, which it cannot do if the
    guard carries its own copy of the argv.
    """
    text = (REPO_ROOT / COMPONENT_DIR / "component.yaml").read_text(encoding="utf-8")
    manifest = parse_manifest(text, f"{COMPONENT_DIR}/component.yaml")
    assert len(manifest.build) == 1, "the guard builds one argv"

    return [_uv(), *manifest.build[0][1:]]


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    """One child, or a skip when the failure was the network."""
    done = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_TIMEOUT_S,
    )
    if done.returncode == 0:
        return

    printed = f"{done.stdout}\n{done.stderr}".lower()
    if any(marker in printed for marker in NETWORK_MARKERS):
        pytest.skip(f"the index was not reachable: {done.stderr[-300:]}")

    raise AssertionError(f"{' '.join(argv)} exited {done.returncode}: {done.stderr[-2000:]}")


@pytest.fixture
def built(tmp_path: Path, uv_available: None) -> tuple[Path, Path]:
    """A copy of the workspace, and a tree built from it the way step 8
    builds one: `uv venv --relocatable` first, then the manifest's argv
    with `UV_PROJECT_ENVIRONMENT` pointed at the staged path.

    Returns `(workspace copy, built tree)`.
    """
    del uv_available
    source = tmp_path / "workspace"
    shutil.copytree(REPO_ROOT, source, ignore=SKIP_COPY, symlinks=True)
    tree = tmp_path / f"{COMPONENT}.new"
    _run([_uv(), *RELOCATABLE_ARGV[1:], str(tree)], cwd=source)
    _run(_build_argv(), cwd=source, env=dict(os.environ) | {VENV_ENV_NAME: str(tree)})

    return source, tree


def _site(tree: Path) -> Path:
    found = sorted(tree.glob(SITE_GLOB))
    assert len(found) == 1, f"expected one site-packages, found {found}"

    return found[0]


def test_the_real_build_leaves_no_pth_naming_anything_outside_the_tree(
    built: tuple[Path, Path],
) -> None:
    """Question 1, asked WITHOUT the module it guards. `escapes` is checked
    beside it, so a bug that made the walk always answer "clean" cannot
    also make this pass."""
    source, tree = built
    site = _site(tree)
    lines = [
        line
        for path in sorted(site.glob("*.pth"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "import "))
    ]

    assert lines == [], lines
    assert str(source) not in "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(site.glob("*.pth"))
    )
    assert escapes(tree) == ()


def test_the_real_build_installs_nothing_editable(built: tuple[Path, Path]) -> None:
    """PEP 610's marker, read straight off the disk. This is the exact
    record `uv sync` writes without `--no-editable`."""
    _, tree = built
    paths = sorted(_site(tree).glob("*.dist-info/direct_url.json"))
    editable = [
        path.parent.name
        for path in paths
        if json.loads(path.read_text(encoding="utf-8")).get("dir_info", {}).get("editable") is True
    ]

    assert paths, "no package recorded a direct_url.json"
    assert editable == [], editable


def test_the_tree_imports_its_own_code_with_the_source_gone(
    built: tuple[Path, Path], tmp_path: Path
) -> None:
    """Question 2, and the one a fake `uv` can never answer.

    The COPY's `release/src` is renamed away, so the only place
    `agent_release` can come from is the tree. The interpreter runs from a
    directory that is neither, because `python -c` puts the working
    directory on `sys.path` and running inside the source would prove
    nothing.
    """
    source, tree = built
    src = source / COMPONENT_DIR / "src"
    hidden = source / COMPONENT_DIR / "src-moved-away"
    src.rename(hidden)
    try:
        done = subprocess.run(
            [
                str(tree / "bin" / "python"),
                "-c",
                f"import {PACKAGE}; print({PACKAGE}.__file__)",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=BUILD_TIMEOUT_S,
        )
    finally:
        hidden.rename(src)

    assert done.returncode == 0, done.stderr
    assert Path(done.stdout.strip()).resolve().is_relative_to(tree.resolve()), done.stdout


def test_every_console_script_the_manifest_needs_is_in_the_tree(
    built: tuple[Path, Path],
) -> None:
    """Contract 06 §4 rule 6: the verify hook ships inside the artifact. A
    non-editable install does not see the source tree beside the module,
    so this asks the built tree rather than the `pyproject.toml`."""
    _, tree = built
    text = (REPO_ROOT / COMPONENT_DIR / "component.yaml").read_text(encoding="utf-8")
    manifest = parse_manifest(text, f"{COMPONENT_DIR}/component.yaml")
    hook = Path(manifest.verify.command[0]).name

    assert (tree / "bin" / hook).is_file(), sorted(one.name for one in (tree / "bin").iterdir())


STALE_PROBE: Final = "SECOND_BUILD_MARKER = 'the code of the second commit'"


def test_a_second_build_from_the_same_checkout_holds_the_new_code(
    built: tuple[Path, Path], tmp_path: Path
) -> None:
    """A second build from one checkout must hold the NEW code. `uv` builds
    a wheel of a local package once, and it rebuilds it only when
    `pyproject.toml`, `setup.py` or `setup.cfg` changes. A deploy changes
    the source and none of those, and every component stays at one version
    for ever, so EVERY later build would install the first wheel again, and
    a release would ship old code under a new commit's name. An editable
    tree never meets this, so only a `--no-editable` build shows it.

    This guard builds twice from ONE checkout with a source edit between, the
    way two deploys do. `built` copies the workspace to a fresh path per test,
    so a single build can never see it.
    """
    source, first = built
    package_init = source / COMPONENT_DIR / "src" / PACKAGE / "__init__.py"
    package_init.write_text(
        package_init.read_text(encoding="utf-8") + f"\n{STALE_PROBE}\n", encoding="utf-8"
    )

    second = tmp_path / f"{COMPONENT}.second"
    _run([_uv(), *RELOCATABLE_ARGV[1:], str(second)], cwd=source)
    _run(_build_argv(), cwd=source, env=dict(os.environ) | {VENV_ENV_NAME: str(second)})

    installed = (_site(second) / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    assert STALE_PROBE in installed, "the second tree holds the FIRST build's code"
    assert STALE_PROBE not in (_site(first) / PACKAGE / "__init__.py").read_text(encoding="utf-8")


def _workspace_members() -> list[str]:
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = root["tool"]["uv"]["workspace"]["members"]
    assert members, "the workspace names no member"

    return [str(one) for one in members]


@pytest.mark.parametrize("member", _workspace_members())
def test_every_workspace_member_keys_its_build_on_its_source(member: str) -> None:
    """The guard above proves the mechanism for one package. This holds EVERY
    member to it, discovered and never listed: any of them can be a workspace
    dependency of a component, and one package without the key is one package
    whose first wheel is installed for ever."""
    body = tomllib.loads((REPO_ROOT / member / "pyproject.toml").read_text(encoding="utf-8"))
    keys = body.get("tool", {}).get("uv", {}).get("cache-keys", [])

    assert {"file": "pyproject.toml"} in keys, member
    assert {"file": "src/**"} in keys, member
    assert (REPO_ROOT / member / "src").is_dir(), (
        f"{member}: the key names src/, and it is not there"
    )
