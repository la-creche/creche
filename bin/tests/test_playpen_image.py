"""`playpen/Dockerfile`, read as text, because no test here builds an image.

The image is the facade: `creche-facade` and `acp-relay` on `PATH`, built
from the facade's lockfile. The build itself runs both programs once and
stops on a program that does not load. These tests hold the file to the
facts that the build cannot check: the names the host runs, the targets the
release script builds, and the programs that Flue's tools run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "playpen" / "Dockerfile"
BUILD = REPO_ROOT / "playpen" / "bin" / "playpen-build"
FACADE_PACKAGE = REPO_ROOT / "facade" / "package.json"

#: Where the build stage deploys the facade, and where each shim points.
DEPLOYED = "/opt/creche-facade"

#: `FROM <image> AS <stage>`.
STAGE_RE = re.compile(r"^FROM \S+ AS (\S+)$", re.MULTILINE)

#: Stages that build a tool for a later stage. A build-only stage ships in
#: no image, so it is not a flavor target.
BUILD_ONLY_STAGES = ("build", "creche-test-venv")


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _stage(name: str) -> str:
    """One stage, from its FROM line to the next FROM line."""
    text = _text()
    start = text.index(f" AS {name}\n")
    end = text.find("\nFROM ", start)

    return text[start:] if end == -1 else text[start:end]


def _facade_bins() -> dict[str, str]:
    return cast("dict[str, str]", json.loads(FACADE_PACKAGE.read_text(encoding="utf-8"))["bin"])


def test_the_targets_are_the_flavors_the_release_builds() -> None:
    """playpen/AGENTS.md rule 6: the targets and the flavors are one list."""
    targets = [name for name in STAGE_RE.findall(_text()) if name not in BUILD_ONLY_STAGES]
    flavors = re.search(r'^FLAVORS="([^"]*)"$', BUILD.read_text(encoding="utf-8"), re.MULTILINE)

    assert flavors is not None
    assert targets == [pair.split(":", 1)[1] for pair in flavors.group(1).split()]
    assert "FROM agent-sandbox AS agent-sandbox-python" in _text()


def test_each_facade_bin_has_a_shim_on_path() -> None:
    """attendance runs both names with `sbx exec`, and the exit test runs
    the same names out of `facade/dist` (integration/proc/proc_exit.py)."""
    stage = _stage("agent-sandbox")
    bins = _facade_bins()

    assert sorted(bins) == ["acp-relay", "creche-facade"]
    assert "for name in creche-facade acp-relay; do" in stage
    assert '> "/usr/local/bin/$name"' in stage
    for path in bins.values():
        assert path.startswith("dist/src/bin/")
        assert path.endswith(".js")
    assert f"exec node {DEPLOYED}/dist/src/bin/%s.js" in stage


def test_the_build_runs_each_program_once() -> None:
    stage = _stage("agent-sandbox")

    assert "creche-facade </dev/null" in stage
    assert "-ne 2 ]" in stage
    assert 'acp-relay </dev/null; [ "$?" = "1" ]' in stage
    assert "AGENT_SANDBOX=" not in stage


def test_the_facade_installs_from_its_frozen_lockfile() -> None:
    """The Flue pin is the one in facade/pnpm-lock.yaml. toybox/ is not read."""
    stage = _stage("build")

    assert "COPY facade/package.json facade/pnpm-lock.yaml facade/pnpm-workspace.yaml ./" in stage
    assert "RUN pnpm install --frozen-lockfile\n" in stage
    assert stage.index("COPY facade/patches ./patches") < stage.index("RUN pnpm install")
    assert f"deploy --prod --frozen-lockfile {DEPLOYED}" in stage
    assert "toybox/" not in _text()


def test_ripgrep_is_in_the_base_stage() -> None:
    """Flue's `grep` runs `rg` when `rg --version` answers. Its `glob` runs
    `find`, which the base image holds."""
    stage = _stage("agent-sandbox")

    assert re.search(r"--no-install-recommends \\\n\s+ripgrep ", stage)


def test_the_image_fixes_no_address() -> None:
    """A sandbox reads the two plane URLs from its env file."""
    assert "AGENT_LAN_ADDRESS" not in _text()
    assert "--build-arg" not in BUILD.read_text(encoding="utf-8")


def test_the_test_venv_installs_the_locked_dependencies() -> None:
    """The python flavor holds a second venv, /opt/test: CPython 3.12 and
    the locked test dependencies of this workspace. The pins come from
    uv.lock, installed at build time, and the workspace's own code stays
    out, so a test imports the code of the clone under test."""
    stage = _stage("creche-test-venv")

    assert "COPY pyproject.toml uv.lock ./" in stage
    assert "uv sync --locked --all-groups --no-install-workspace" in stage
    assert "COPY --from=creche-test-venv /opt/test /opt/test" in _text()
    assert "FROM python:3.12-slim-bookworm AS creche-test-venv" in _text()


def test_the_image_ships_no_installer() -> None:
    """A sandbox runs code and installs nothing. pip leaves the analysis
    venv in the RUN that installs its wheels, and uv, pip and ensurepip
    leave the interpreter the test venv uses. The self-test fails the
    build when one of them comes back."""
    stage = _stage("agent-sandbox-python")
    venv_stage = _stage("creche-test-venv")

    assert "/opt/py/bin/pip uninstall -y pip" in stage
    assert "rm -rf /usr/local/lib/python3.12/site-packages/pip*" in venv_stage
    assert "/usr/local/lib/python3.12/ensurepip" in venv_stage
    assert 'ENV PATH="/opt/py/bin:/opt/test/bin:${PATH}"' in stage
    assert "/opt/test/bin/pytest --version" in stage
    assert "the image ships pip in /usr/local" in stage
    assert "the test venv ships pip" in stage
