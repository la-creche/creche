"""`disks/bin/playpen-release-disks`, with stand-ins for `docker`, `curl`
and `sha256sum`, and no image (decisions CH7-2, CH41 rule 1, CH76 rule 5).

The script builds the files of the disks row with the three playpen
scripts. The `disks` job of release.yml runs it for real when the plan
tags `disks`, and the playpen-image job builds each disk on each PR. Here
the stand-in `docker` writes the bytes that each container would write:
a tar with `etc/creche/env`, a raw disk and its compressed copy, and the
compressed kernel. The stand-in `sha256sum` gives the pinned digest for
the fetched kernel, so playpen-kernel passes its pin check, and the real
digest of each other file.

The tests hold the output to three other sources:

1. `artifacts.disk_files`: the four files that `handover disk-assets`
   names, and so the files that root installs.
2. `rust/crates/wire/vectors/digests.json`: the keys and the file names of
   `digests.json`. The vmm parses the same vector
   (`rust/crates/vmm/src/image_root/tests.rs`).
3. `catalog.DISK_FLAVORS` and the flavor pairs of `playpen/bin/playpen-ci`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Final

import pytest
from handover.artifacts import disk_files
from handover.catalog import DIGESTS_FILE, DISK_FLAVORS, DISK_SUFFIX, KERNEL_FILE
from handover.discovery import read_one

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "disks" / "bin" / "playpen-release-disks"
PLAYPEN_CI: Final = REPO / "playpen" / "bin" / "playpen-ci"
KERNEL_SCRIPT: Final = REPO / "playpen" / "bin" / "playpen-kernel"
VECTOR: Final = REPO / "rust" / "crates" / "wire" / "vectors" / "digests.json"


def _kernel_pin() -> str:
    """The pinned SHA-256 of the kernel, as playpen-kernel holds it."""
    found = re.search(
        r"^SHA256=([0-9a-f]{64})$", KERNEL_SCRIPT.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert found is not None
    return found.group(1)


KERNEL_PIN: Final = _kernel_pin()

#: The digest list that the script writes beside the four files.
SUMS: Final = "SHA256SUMS"

#: `docker`: one argv line per call into STUB_LOG.
#: - `buildx build ... --target T ... --output type=tar,dest=D`: a tar at D
#:   with `etc/creche/env`.
#: - `run ... -v W:/out TOOLS sh ... NAME SIZE`: reads stdin, writes
#:   `W/NAME.ext4` (`raw NAME`) and `W/NAME.ext4.zst` (`zst NAME`).
#: - `run ... -v W:/out TOOLS zstd ... -o /out/F /out/K`: writes `W/F`.
#: - `build`: nothing. A call whose second word is STUB_FAIL fails.
DOCKER_STUB = """#!/usr/bin/env bash
line="$*"
printf '%s\\n' "${line//$'\\n'/ }" >> "$STUB_LOG"
if [[ "$1" == buildx ]]; then
  [[ "$*" == *"--target ${STUB_FAIL:-none} "* ]] && exit 1
  for word in "$@"; do
    case "$word" in type=tar,dest=*) dest="${word#type=tar,dest=}" ;; esac
  done
  tree="$(mktemp -d)"
  mkdir -p "$tree/etc/creche"
  printf 'PATH=/usr/bin\\n' > "$tree/etc/creche/env"
  tar -cf "$dest" -C "$tree" etc
  rm -rf -- "$tree"
  exit 0
fi
if [[ "$1" == run ]]; then
  while [[ "$1" != -v ]]; do shift; done
  dir="${2%%:*}"
  if [[ "$4" == zstd ]]; then
    out="${@: -2:1}"
    printf 'zst kernel\\n' > "$dir/${out#/out/}"
    exit 0
  fi
  cat > /dev/null
  name="${@: -2:1}"
  [[ "$name" == "${STUB_FAIL:-none}" ]] && exit 1
  printf 'raw %s\\n' "$name" > "$dir/$name.ext4"
  printf 'zst %s\\n' "$name" > "$dir/$name.ext4.zst"
fi
exit 0
"""

#: `curl`: writes `a kernel` to the path after `--output`.
CURL_STUB = """#!/usr/bin/env bash
while [[ "$1" != --output ]]; do shift; done
printf 'a kernel\\n' > "$2"
"""

#: `sha256sum`: the pin for the fetched kernel, the real digest of each
#: other file. It takes the `--` of the script.
SHA256SUM_STUB = f"""#!/usr/bin/env python3
import hashlib, sys
for path in sys.argv[1:]:
    if path == "--":
        continue
    if path.endswith("/vmlinux-x86_64"):
        print("{KERNEL_PIN}  " + path)
        continue
    with open(path, "rb") as file:
        print(hashlib.sha256(file.read()).hexdigest() + "  " + path)
"""


def _stub(directory: Path, name: str, text: str) -> None:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _run(
    tmp_path: Path, fail: str = "", out: Path | None = None
) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    _stub(stubs, "docker", DOCKER_STUB)
    _stub(stubs, "curl", CURL_STUB)
    _stub(stubs, "sha256sum", SHA256SUM_STUB)
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "docker.log"),
        "STUB_FAIL": fail,
        "SOURCE_DATE_EPOCH": "1700000000",
        "DISK_OUT": str(out if out is not None else tmp_path / "out"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        timeout=60,
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _entry(value: object) -> dict[str, str]:
    """One entry of `digests.json`: an object of texts."""
    assert isinstance(value, dict)
    return {str(key): str(text) for key, text in value.items()}


def _written(tmp_path: Path) -> dict[str, object]:
    done = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    loaded: dict[str, object] = json.loads(
        (tmp_path / "out" / DIGESTS_FILE).read_text(encoding="utf-8")
    )
    return loaded


def test_the_script_writes_one_file_per_flavor_and_the_kernel(tmp_path: Path) -> None:
    """The four files of `handover disk-assets` and SHA256SUMS, nothing
    else. SHA256SUMS holds the digest of each file as it ships."""
    done = _run(tmp_path)

    assert done.returncode == 0, done.stderr
    out = tmp_path / "out"
    names = [one.name for one in disk_files()]
    assert sorted(path.name for path in out.iterdir()) == sorted([*names, SUMS])
    assert (out / SUMS).read_text(encoding="utf-8") == "".join(
        f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}\n" for name in names
    )


def test_digests_json_has_the_vector_keys(tmp_path: Path) -> None:
    """The keys and the file names of the shared vector, each entry
    `{file, sha256}`, in the byte form of the vector: compact, keys
    sorted, one line feed (decision CH58 rule 4)."""
    written = _written(tmp_path)
    text = tmp_path / "out" / DIGESTS_FILE
    vector = json.loads(VECTOR.read_text(encoding="utf-8"))

    assert {key: _entry(entry)["file"] for key, entry in written.items()} == {
        key: _entry(entry)["file"] for key, entry in vector.items()
    }
    for one in (written, vector):
        assert all(set(_entry(entry)) == {"file", "sha256"} for entry in one.values())
    for path in (text, VECTOR):
        body = path.read_text(encoding="utf-8")
        assert body == json.dumps(json.loads(body), separators=(",", ":"), sort_keys=True) + "\n"


def test_the_digest_lines_are_the_decompressed_digests(tmp_path: Path) -> None:
    """Each sha256 is the digest of the raw bytes, not of the shipped `.zst`
    (decision CH41 rule 1). The kernel has its pin."""
    written = _written(tmp_path)

    assert written == {
        "kernel": {"file": KERNEL_FILE, "sha256": KERNEL_PIN},
        **{
            flavor: {"file": f"{flavor}{DISK_SUFFIX}", "sha256": _sha(f"raw {flavor}\n")}
            for flavor in DISK_FLAVORS
        },
    }
    zst = (tmp_path / "out" / f"{DISK_FLAVORS[0]}{DISK_SUFFIX}").read_text(encoding="utf-8")
    assert zst == f"zst {DISK_FLAVORS[0]}\n"


def test_the_flavors_are_the_disk_flavors_and_the_targets_of_playpen_ci(tmp_path: Path) -> None:
    """One root disk for each flavor of `catalog.DISK_FLAVORS`, in that
    order, each from its target in playpen-ci (decision CH76 rule 7)."""
    pairs = re.search(r'^FLAVORS="([^"]*)"$', SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    ci = re.search(r'^FLAVORS="([^"]*)"$', PLAYPEN_CI.read_text(encoding="utf-8"), re.MULTILINE)
    assert pairs is not None
    assert ci is not None
    ours = dict(pair.split(":") for pair in pairs.group(1).split())
    theirs = dict(pair.split(":") for pair in ci.group(1).split())

    assert tuple(ours) == DISK_FLAVORS
    assert ours == {flavor: theirs[flavor] for flavor in DISK_FLAVORS}

    done = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    log = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    targets = [line.split("--target ")[1].split()[0] for line in log if line.startswith("buildx ")]
    assert targets == list(ours.values())


def test_the_manifest_build_runs_this_script() -> None:
    """The one build argv of `disks/component.toml` names the script."""
    (argv,) = read_one(REPO, "disks").manifest.build

    assert argv[-1] == str(SCRIPT.relative_to(REPO))


@pytest.mark.parametrize("fail", ["agent-sandbox-python", "base"])
def test_a_failed_step_leaves_disk_out_empty(tmp_path: Path, fail: str) -> None:
    """A failed tar or a failed disk stops the run, and DISK_OUT holds no
    file: the job uploads nothing to sign."""
    done = _run(tmp_path, fail=fail)

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1].startswith("playpen-release-disks: the ")
    assert list((tmp_path / "out").iterdir()) == []


def test_a_disk_out_that_holds_a_file_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "old").write_text("old\n", encoding="utf-8")

    done = _run(tmp_path, out=out)

    assert done.returncode == 1
    assert done.stderr == "playpen-release-disks: DISK_OUT is not empty\n"
    assert not (tmp_path / "docker.log").exists()
