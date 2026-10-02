"""The seal, settled against a REAL `sops`.

`host.seal_argv` hands the plaintext to `sops` on `/dev/stdin`, and only a
test that runs the real command can confirm that stdin handling. `sops`
3.13.3 and `age` are installed on this build machine, so these tests run the
real child. Every test skips, loudly, where either tool is missing.

They are `slow` because each one starts real children.

Four facts, and the third is why `seal_argv` carries `--config /dev/null`.

1. `/dev/stdin` works. The plaintext goes in on stdin, sealed bytes come
   back on stdout, and the plaintext is absent from what comes back.
2. `sops --decrypt` reads the result, so the PEP's loader still works on
   both ends, which is the one hard constraint on the command.
3. **A `.sops.yaml` anywhere above the child's working directory decides
   whether `sops` will run at all.** With a config whose `creation_rules`
   do not match `/dev/stdin`, `sops` exits 1 with "no matching creation
   rules found" and seals nothing, EVEN THOUGH `--age` names the
   recipients on the command line. The intake's child inherits root's
   working directory, and the platform checkouts under
   `/srv/agents/work/platform/` each carry a `.sops.yaml` that the
   attacker writes. That is a wedge on every future paste, for the price
   of one file. `--config /dev/null` closes it.
4. Command-line `--age` WINS over a discovered config's own recipients, so
   a planted file cannot add a reader. That is worth a test of its own,
   because it is the opposite failure and the code would look identical.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from agent_release.executor.host import seal_argv

pytestmark = pytest.mark.slow

VALUE: bytes = b"hunter2-the-pasted-value"

#: A child environment with nothing of the test runner's in it, which is
#: what `host.child_env` builds for the real sealer too.
CHILD_ENV: dict[str, str] = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}

SEAL_TIMEOUT_S: float = 30.0


def _tools() -> tuple[str, str]:
    """`sops` and `age-keygen`, or a skip that names the missing one."""
    sops = shutil.which("sops")
    keygen = shutil.which("age-keygen")
    if sops is None or keygen is None:
        missing = "sops" if sops is None else "age-keygen"
        pytest.skip(f"{missing} is not on this machine, so the seal cannot be run here")

    return sops, keygen


def _keypair(keygen: str, tmp_path: Path, name: str = "key") -> tuple[str, Path]:
    """One throwaway age key. The public half seals, the private half is
    only ever used to prove the round trip."""
    key_file = tmp_path / f"{name}.txt"
    made = subprocess.run(
        [keygen, "-o", str(key_file)], capture_output=True, timeout=SEAL_TIMEOUT_S, check=True
    )
    public = made.stderr.decode("utf-8").split(": ", 1)[1].strip()

    return public, key_file


def _seal(sops: str, public: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """`make_sealer`'s child, with `sops` in place of the absolute path the
    host uses and `cwd` in place of whatever root's happens to be."""
    argv = [sops, *seal_argv((public,))[1:]]

    return subprocess.run(
        argv,
        input=VALUE,
        env=CHILD_ENV,
        cwd=str(cwd),
        capture_output=True,
        timeout=SEAL_TIMEOUT_S,
        check=False,
    )


def _config(directory: Path, pattern: str, recipient: str) -> None:
    body = f"creation_rules:\n  - path_regex: {pattern}\n    age: {recipient}\n"
    (directory / ".sops.yaml").write_text(body, encoding="utf-8")


def test_sops_seals_what_it_reads_on_dev_stdin(tmp_path: Path) -> None:
    sops, keygen = _tools()
    public, _ = _keypair(keygen, tmp_path)

    done = _seal(sops, public, tmp_path)

    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert done.stdout
    assert VALUE not in done.stdout


def test_the_pep_can_decrypt_what_the_sealer_wrote(tmp_path: Path) -> None:
    sops, keygen = _tools()
    public, key_file = _keypair(keygen, tmp_path)
    sealed = _seal(sops, public, tmp_path)
    assert sealed.returncode == 0, sealed.stderr.decode("utf-8", "replace")

    stored = tmp_path / "weather_token.enc"
    stored.write_bytes(sealed.stdout)
    read_back = subprocess.run(
        [sops, "--decrypt", "--input-type", "binary", "--output-type", "binary", str(stored)],
        env=CHILD_ENV | {"SOPS_AGE_KEY_FILE": str(key_file)},
        cwd=str(tmp_path),
        capture_output=True,
        timeout=SEAL_TIMEOUT_S,
        check=False,
    )

    assert read_back.returncode == 0, read_back.stderr.decode("utf-8", "replace")
    assert read_back.stdout == VALUE


def test_a_planted_sops_config_cannot_wedge_the_seal(tmp_path: Path) -> None:
    """Fact 3. A `.sops.yaml` with no rule for `/dev/stdin` would make
    every seal exit 1, so every future paste would answer 500."""
    sops, keygen = _tools()
    public, _ = _keypair(keygen, tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _config(tmp_path, r"\.enc$", public)

    done = _seal(sops, public, work)

    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert done.stdout


def test_a_planted_sops_config_cannot_add_a_recipient(tmp_path: Path) -> None:
    """Fact 4. `--age` on the command line is the recipient set, whole."""
    sops, keygen = _tools()
    ours, _ = _keypair(keygen, tmp_path, "ours")
    theirs, _ = _keypair(keygen, tmp_path, "theirs")
    work = tmp_path / "work"
    work.mkdir()
    _config(tmp_path, ".*", theirs)

    done = _seal(sops, ours, work)

    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    recipients = [one["recipient"] for one in json.loads(done.stdout)["sops"]["age"]]
    assert recipients == [ours]
