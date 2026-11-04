"""`playpen/bin/playpen-rootfs`, with a `docker` stub and no image (CH7-2,
fact 59).

The stub records its argv and writes the tar that `--output type=tar`
names, with the members that STUB_MEMBERS lists. CI runs the real script on
each flavor through `playpen/bin/playpen-ci`.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOTFS = REPO_ROOT / "playpen" / "bin" / "playpen-rootfs"

#: `docker`: one argv line per call. It makes a tar of STUB_MEMBERS at the
#: `dest=` of the `--output` argument. `STUB_FAIL=1` fails the build.
DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
[[ -n "${STUB_FAIL:-}" ]] && exit 1
for one in "$@"; do
  [[ "$one" == type=tar,dest=* ]] && dest="${one#type=tar,dest=}"
done
tree="$(mktemp -d)"
for member in $STUB_MEMBERS; do
  mkdir -p "$tree/$(dirname "$member")"
  : > "$tree/$member"
done
tar -cf "$dest" -C "$tree" .
"""


def _run(
    tmp_path: Path, members: str, *, fail: bool = False, epoch: str = "1700000000"
) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    docker = stubs / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "docker.log"),
        "STUB_MEMBERS": members,
        "STUB_FAIL": "1" if fail else "",
        "SOURCE_DATE_EPOCH": epoch,
    }
    return subprocess.run(
        ["bash", str(ROOTFS), "agent-sandbox", str(tmp_path / "out.tar")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_tar_of_the_target_is_written(tmp_path: Path) -> None:
    done = _run(tmp_path, "etc/creche/env usr/bin/node")

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "out.tar").is_file()
    assert not (tmp_path / "out.tar.partial").exists()
    (build,) = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert build.startswith(
        "buildx build --platform linux/amd64 -f "
        f"{REPO_ROOT}/playpen/Dockerfile --target agent-sandbox "
        "--build-arg SOURCE_DATE_EPOCH=1700000000 "
        f"--output type=tar,dest={tmp_path / 'out.tar.partial'} "
    )


@pytest.mark.parametrize(
    ("members", "fail", "error"),
    [
        ("usr/bin/node", False, "the agent-sandbox tar holds no etc/creche/env"),
        ("etc/creche/env .dockerenv", False, "the agent-sandbox tar holds .dockerenv"),
        ("etc/creche/env", True, "the agent-sandbox build failed"),
    ],
)
def test_a_failed_check_leaves_no_tar(tmp_path: Path, members: str, fail: bool, error: str) -> None:
    (tmp_path / "out.tar").write_bytes(b"an older run")

    done = _run(tmp_path, members, fail=fail)

    assert done.returncode == 1
    assert done.stderr.endswith(f"playpen-rootfs: {error}\n")
    assert not (tmp_path / "out.tar").exists()
    assert not (tmp_path / "out.tar.partial").exists()


def test_an_epoch_that_is_no_count_of_seconds_is_refused(tmp_path: Path) -> None:
    done = _run(tmp_path, "etc/creche/env", epoch="yesterday")

    assert done.returncode == 1
    assert done.stderr == "playpen-rootfs: SOURCE_DATE_EPOCH is not a count of seconds: yesterday\n"
    assert not (tmp_path / "docker.log").exists()
