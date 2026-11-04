"""`playpen/bin/playpen-disk`, with a `docker` stub and no image (CH7-2).

The stub records each argv and, for `run`, writes the compressed disk and
the raw disk that the real container would write. CI runs the real script
on each flavor, two times, through `playpen/bin/playpen-ci`.
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DISK = REPO_ROOT / "playpen" / "bin" / "playpen-disk"
UUID = "e40ec111-ff66-4afb-9226-7ea87cc0fe41"
RAW = b"raw ext4 bytes\n"

#: `docker`: one argv line per call, line breaks as spaces. `run` reads stdin to the end, then
#: writes `<name>.ext4` and `<name>.ext4.zst` into the folder after `-v`.
DOCKER_STUB = """#!/usr/bin/env bash
line="$*"
printf '%s\\n' "${line//$'\\n'/ }" >> "$STUB_LOG"
[[ "$1" == "${STUB_FAIL:-}" ]] && exit 1
if [[ "$1" == run ]]; then
  cat > /dev/null
  while [[ "$1" != -v ]]; do shift; done
  dir="${2%%:*}"
  name="${@: -2:1}"
  printf 'raw ext4 bytes\\n' > "$dir/$name.ext4"
  printf 'zst\\n' > "$dir/$name.ext4.zst"
fi
exit 0
"""


def _run(
    tmp_path: Path, *argv: str, fail: str = "", tar_bytes: int = 4096
) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    docker = stubs / "docker"
    docker.write_text(DOCKER_STUB, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "rootfs.tar").write_bytes(b"\0" * tar_bytes)
    (tmp_path / "out").mkdir(exist_ok=True)
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "docker.log"),
        "STUB_FAIL": fail,
        "SOURCE_DATE_EPOCH": "1700000000",
    }
    return subprocess.run(
        ["bash", str(DISK), *argv],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )


def _log(tmp_path: Path) -> list[str]:
    return (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()


def test_the_disk_is_written_and_its_raw_digest_printed(tmp_path: Path) -> None:
    done = _run(tmp_path, str(tmp_path / "rootfs.tar"), str(tmp_path / "out"), "base")

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{hashlib.sha256(RAW).hexdigest()}  base.ext4\n"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["base.ext4.zst"]


def test_the_container_gets_the_fixed_inputs(tmp_path: Path) -> None:
    """The UUID, the time and the size follow from fixed values and the
    tar size alone: 4096 bytes plus a half plus 256 MiB is 257 MiB."""
    done = _run(tmp_path, str(tmp_path / "rootfs.tar"), str(tmp_path / "out"), "base")

    assert done.returncode == 0, done.stderr
    build, run = _log(tmp_path)
    assert build.startswith("build --platform linux/amd64 -q -t playpen-disk-tools ")
    assert build.endswith("/playpen/disk")
    assert run.startswith("run --rm -i --network none --platform linux/amd64 ")
    assert "-e E2FSPROGS_FAKE_TIME=1700000000 -e SOURCE_DATE_EPOCH=1700000000" in run
    assert "-e LC_ALL=C.UTF-8" in run
    assert run.endswith(f" disk {UUID} base 257")


def test_a_relative_out_dir_becomes_an_absolute_mount(tmp_path: Path) -> None:
    """docker -v reads a relative source as a volume name, not a folder."""
    done = _run(tmp_path, "rootfs.tar", "out", "base")

    assert done.returncode == 0, done.stderr
    _, run = _log(tmp_path)
    mount = run.split(" -v ", 1)[1].split(":", 1)[0]
    assert mount.startswith("/")
    assert Path(mount).parent.resolve() == (tmp_path / "out").resolve()


@pytest.mark.parametrize(
    ("fail", "error"),
    [("build", "the disk-tools build failed"), ("run", "the disk of base failed")],
)
def test_a_failed_step_leaves_no_disk(tmp_path: Path, fail: str, error: str) -> None:
    done = _run(tmp_path, str(tmp_path / "rootfs.tar"), str(tmp_path / "out"), "base", fail=fail)

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.endswith(f"playpen-disk: {error}\n")
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (("missing.tar", "out", "base"), "no tar at missing.tar"),
        (("rootfs.tar", "missing", "base"), "no folder at missing"),
        (("rootfs.tar", "out", "Base"), "the name Base is not a lowercase word"),
        (("rootfs.tar", "out"), "usage: playpen-disk <rootfs.tar> <out-dir> <name>"),
    ],
)
def test_the_script_refuses_bad_arguments(
    tmp_path: Path, argv: tuple[str, ...], error: str
) -> None:
    paths = [
        str(tmp_path / one) if one.endswith((".tar", "out", "missing")) else one for one in argv
    ]
    want = error.replace("missing.tar", str(tmp_path / "missing.tar")).replace(
        "at missing", f"at {tmp_path / 'missing'}"
    )

    done = _run(tmp_path, *paths)

    assert done.returncode == 1
    assert done.stderr == f"playpen-disk: {want}\n"
