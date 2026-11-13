"""`playpen/Dockerfile`, read as text, because no test here builds an image.

The image is the facade: `creche-facade` on `PATH`, built from the
facade's lockfile. The build itself runs the facade once and stops when it
does not load. These tests hold the file to the
facts that the build cannot check: the names the host runs, the targets the
release script builds, and the programs that Flue's tools run.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "playpen" / "Dockerfile"
BUILD = REPO_ROOT / "playpen" / "bin" / "playpen-build"
CI = REPO_ROOT / "playpen" / "bin" / "playpen-ci"
DISK = REPO_ROOT / "playpen" / "bin" / "playpen-disk"
GUEST_INIT_BOOT = REPO_ROOT / "rust" / "crates" / "guest-init" / "src" / "boot.rs"
GUEST_VECTORS = REPO_ROOT / "rust" / "crates" / "wire" / "vectors" / "guest.json"
FACADE_PACKAGE = REPO_ROOT / "facade" / "package.json"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"

#: Where the build stage deploys the facade, and where each shim points.
DEPLOYED = "/opt/creche-facade"

#: `FROM <image> AS <stage>`.
STAGE_RE = re.compile(r"^FROM \S+ AS (\S+)$", re.MULTILINE)

#: Stages that build a tool for a later stage. A build-only stage ships in
#: no image, so it is not a flavor target.
BUILD_ONLY_STAGES = ("build", "creche-test-venv")

#: Flavors that ship as a root disk only: CI builds them, playpen-build does
#: not, and caregiver maps them to no sbx image (CH7-1, CH53 rule 1, CH79
#: rule 1).
DISK_ONLY = ("coding:agent-sandbox-coding",)


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
    checked = re.search(r'^FLAVORS="([^"]*)"$', CI.read_text(encoding="utf-8"), re.MULTILINE)

    assert flavors is not None
    assert checked is not None
    assert targets == [pair.split(":", 1)[1] for pair in checked.group(1).split()]
    assert checked.group(1) == flavors.group(1) + " " + " ".join(DISK_ONLY)
    assert "FROM agent-sandbox AS agent-sandbox-python" in _text()


def test_the_facade_bin_has_a_shim_on_path() -> None:
    """guest-init runs `creche-facade` at `FACADE_PROGRAM`
    (rust/crates/wire/src/guest.rs). It is the one bin of the facade."""
    stage = _stage("agent-sandbox")

    assert _facade_bins() == {"creche-facade": "dist/src/bin/creche-facade.js"}
    assert "> /usr/local/bin/creche-facade" in stage
    assert f"exec node {DEPLOYED}/dist/src/bin/creche-facade.js" in stage


def test_the_build_runs_the_facade_once() -> None:
    """With no `CRECHE_SESSION_ID` the facade refuses with 17
    (facade/AGENTS.md, "Exit status")."""
    stage = _stage("agent-sandbox")

    assert 'err="$(creche-facade </dev/null 2>&1 >/dev/null)"' in stage
    assert "-ne 17 ]" in stage
    assert "creche-facade: CRECHE_SESSION_ID is not set" in stage
    assert "AGENT_" not in stage


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
    """The two planes are fixed on guest loopback (agent/src/guest-start.ts)."""
    assert "AGENT_LAN_ADDRESS" not in _text()
    assert "--build-arg" not in BUILD.read_text(encoding="utf-8")


def test_the_test_venv_installs_the_locked_dependencies() -> None:
    """The python flavor holds a second venv, /opt/test: CPython 3.12 and
    the locked test dependencies of this workspace. The pins come from
    uv.lock, installed at build time, and the workspace's own code stays
    out, so a test imports the code of the clone under test."""
    stage = _stage("creche-test-venv")
    groups = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["dependency-groups"]

    assert "COPY pyproject.toml uv.lock ./" in stage
    assert (
        "uv sync --locked --no-build --no-default-groups --group sandbox-test "
        "--no-install-workspace" in stage
    )
    assert {"include-group": "sandbox-test"} in groups["dev"]
    assert all(
        not requirement.startswith(("nodeenv", "pyright", "ruff"))
        for requirement in groups["sandbox-test"]
    )
    assert "COPY --from=creche-test-venv /opt/test /opt/test" in _text()
    assert "FROM ${PYTHON_IMAGE} AS creche-test-venv" in _text()


def test_the_test_venv_copies_every_workspace_member() -> None:
    """`uv sync --locked` resolves the whole workspace. A member whose
    pyproject.toml the stage does not copy is a source with no member, and
    uv refuses the sync, so the image does not build."""
    stage = _stage("creche-test-venv")
    members = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["uv"]["workspace"][
        "members"
    ]

    for member in members:
        assert f"COPY {member}/pyproject.toml {member}/" in stage, member


def test_the_build_inputs_are_content_pinned() -> None:
    text = _text()
    venv_stage = _stage("creche-test-venv")

    assert re.search(r"^ARG NODE_IMAGE=node:24-bookworm@sha256:[0-9a-f]{64}$", text, re.MULTILINE)
    assert re.search(
        r"^ARG PYTHON_IMAGE=python:3\.12-slim-bookworm@sha256:[0-9a-f]{64}$",
        text,
        re.MULTILINE,
    )
    assert "FROM ${NODE_IMAGE} AS build" in text
    assert "FROM ${NODE_IMAGE} AS agent-sandbox" in text
    assert re.search(r"^ARG UV_WHEEL_SHA256=[0-9a-f]{64}$", text, re.MULTILINE)
    assert "sha256sum -c -" in venv_stage
    assert "pip install --no-cache-dir --no-deps" in venv_stage


def test_the_final_stage_uses_its_own_system_libraries() -> None:
    stage = _stage("agent-sandbox-python")

    assert "libssl3 libsqlite3-0" in stage
    assert "COPY --from=creche-test-venv /usr/local/lib/libssl.so.3" not in stage
    assert "COPY --from=creche-test-venv /usr/local/lib/libcrypto.so.3" not in stage
    assert "COPY --from=creche-test-venv /usr/lib/x86_64-linux-gnu/libsqlite3.so.0" not in stage


def test_the_final_stage_keeps_the_interpreter_links() -> None:
    stage = _stage("agent-sandbox-python")

    assert "COPY --from=creche-test-venv /usr/local/bin/python /usr/local/bin/" in stage
    assert "COPY --from=creche-test-venv /usr/local/bin/python3 /usr/local/bin/" in stage
    assert "COPY --from=creche-test-venv /usr/local/bin/python3.12 /usr/local/bin/" in stage


def test_the_wrappers_do_not_import_from_the_current_directory() -> None:
    venv_stage = _stage("creche-test-venv")
    final_stage = _stage("agent-sandbox-python")

    assert "exec /opt/test/bin/python -P -c" in venv_stage
    assert "from creche_env.cli import main" not in final_stage


def test_the_test_venv_dependencies_hold_the_security_fixes() -> None:
    packages = {
        package["name"]: package["version"]
        for package in tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]
        if "version" in package
    }

    assert tuple(map(int, packages["certifi"].split("."))) >= (2026, 10, 18)


def test_the_image_ships_no_installer() -> None:
    """A sandbox runs code and installs nothing. pip leaves the analysis
    venv in the RUN that installs its wheels, and uv, pip and ensurepip
    leave the interpreter the test venv uses. The self-test fails the
    build when one of them comes back."""
    stage = _stage("agent-sandbox-python")
    venv_stage = _stage("creche-test-venv")

    assert "/opt/py/bin/pip uninstall -y pip" in stage
    assert "apt-get purge -y --auto-remove" in stage
    assert "python3-venv python3.11-venv" in stage
    assert "python3-pip-whl python3-setuptools-whl" in stage
    assert "/usr/lib/python3.11/ensurepip" in stage
    assert "/usr/share/python-wheels" in stage
    assert "rm -rf /usr/local/lib/python3.12/site-packages/pip*" in venv_stage
    assert "/usr/local/lib/python3.12/site-packages/uv*" in venv_stage
    assert "/usr/local/lib/python3.12/ensurepip" in venv_stage
    assert 'ENV PATH="/opt/py/bin:/opt/test/bin:${PATH}"' in stage
    assert "/opt/test/bin/pytest --version" in stage
    assert "the image ships pip in /usr/local" in stage
    assert "the image ships uv in /usr/local" in stage
    assert "the test venv ships pip" in stage
    assert "the analysis interpreter ships ensurepip" in stage
    assert "the test venv ships nodeenv" in stage


def test_no_flavor_sets_a_cmd() -> None:
    """Fact 59: sbx gives each sandbox its own command, and a root disk
    carries no image config, so a CMD line would only mislead."""
    assert re.search(r"^CMD ", _text(), re.MULTILINE) is None


def _session_mount() -> str:
    """The session mount of creche_wire::guest, read from its vectors file,
    the bridge to the other languages (CH62 rule 5)."""
    vectors = cast("dict[str, object]", json.loads(GUEST_VECTORS.read_text(encoding="utf-8")))
    session = vectors["session_disk_mount"]
    assert isinstance(session, str)

    return session


def _tmpfs_points() -> list[str]:
    """The tmpfs dirs of the boot plan of guest init, as many as the type
    of TMPFS names (CH62 rule 4)."""
    boot = GUEST_INIT_BOOT.read_text(encoding="utf-8")
    tmpfs = re.search(r"const TMPFS: \[\(&str, &CStr\); (\d+)\] = \[(.*?)\];", boot, re.DOTALL)
    assert tmpfs is not None
    points = re.findall(r'\("(/[^"]+)", c"mode=', tmpfs.group(2))
    assert len(points) == int(tmpfs.group(1))

    return points


def _kernel_points() -> list[str]:
    """The kernel mount points that plan() puts on the root disk: proc,
    sysfs and the move of /dev (CH62 rule 4)."""
    boot = GUEST_INIT_BOOT.read_text(encoding="utf-8")
    plan = re.search(r"pub fn plan\(\) -> Vec<Step> \{(.*?)\n\}\n", boot, re.DOTALL)
    assert plan is not None

    points = re.findall(r'staged\("(/[^"]+)"\)', plan.group(1))
    assert points

    return points


def test_the_base_stage_makes_the_session_mount_point() -> None:
    """CH62 rule 5: guest init mounts the session disk and makes no dir, so
    the root disk holds the session mount as root:root 0755. The python
    and coding stages build on this stage, so each flavor holds it."""
    stage = _stage("agent-sandbox")
    session = _session_mount()
    text = _text()

    assert f"RUN mkdir -p -m 0755 {session} \\\n" in stage
    assert "USER " not in text
    assert "FROM agent-sandbox AS agent-sandbox-python" in text
    assert "FROM agent-sandbox-python AS agent-sandbox-coding" in text
    for stage_name in ("agent-sandbox-python", "agent-sandbox-coding"):
        assert session not in _stage(stage_name)


def test_each_mount_point_of_guest_init_is_named_in_the_image_and_the_disk_check() -> None:
    """A mount point the boot plan adds fails here until the playpen-disk
    loop lists it, and a tmpfs dir until the Dockerfile loop lists it too.
    The build mounts /dev, /proc and /sys in each RUN step, so only the
    disk check can see those. A missing dir on the read-only root is a
    boot fault, so CI stops it before a release."""
    tmpfs = _tmpfs_points()
    kernel = _kernel_points()
    dockerfile_loop = re.search(r"for dir in ([^;]+); do", _stage("agent-sandbox"))
    disk_loop = re.search(r"for dir in ([^;]+); do", DISK.read_text(encoding="utf-8"))

    assert dockerfile_loop is not None
    assert disk_loop is not None
    assert dockerfile_loop.group(1).split() == tmpfs
    assert sorted(disk_loop.group(1).split()) == sorted([*kernel, *tmpfs, _session_mount()])
