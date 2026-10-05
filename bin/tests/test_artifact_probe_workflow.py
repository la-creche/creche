"""The artifact probe runs by hand, measures on `main` only and can sign in
one job that holds no code of the repository.

`.github/workflows/artifact-probe.yml` builds a static program, signs its
archive with a keyless signature and checks that signature inside
`systemd-run`. `bin/artifact-probe.sh` holds the measurement. A probe run
writes one entry to a public log that nobody can change, and no pull request
can start the workflow before the merge. So these tests are the only run that
the two files get first.

The first half pins the workflow file:

1. One trigger, `workflow_dispatch`. A run on a pull request head counts for
   the release of that pull request
   (`handover/src/handover/executor/provenance.py`, P5).
2. `guard` always runs. Each other job runs on `main` only and needs `guard`,
   so a run on another ref is a success with three skipped jobs.
3. Only `sign` holds the identity token, and it holds nothing else: no
   checkout, no local action and no script of the repository.
4. The whole text of the step that takes cosign. `sign` and `verify` hold the
   same text, and the SHA-256 compare is before the first use of the program.
5. No step makes a tag, a Release or a push.

The second half runs the bash text: the two steps that guard the signature,
and then the script. Each program that only a runner has is a stub: `curl`,
`sudo` and what runs behind it, `rustup`, `cargo`, `file` and `systemctl`. A
stub writes the words that it got to a log, and the tests read the words
from there. The cases that pack an archive need GNU tar, two cases need
`sha256sum --check --strict`, and the cases that read a report need jq. On a
development machine without such a tool, its cases skip. In CI no case
skips: a case whose tool is absent fails there.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO: Final = Path(__file__).resolve().parents[2]
WORKFLOW: Final = REPO / ".github" / "workflows" / "artifact-probe.yml"
SCRIPT: Final = REPO / "bin" / "artifact-probe.sh"
SCRIPT_TEXT: Final = SCRIPT.read_text(encoding="utf-8")
PROBE: Final = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
JOBS: Final[dict[str, dict[str, Any]]] = PROBE["jobs"]

#: The workflow's name, and the stem of its file. A certificate names the
#: file, and the script checks for that name.
PROBE_NAME: Final = "artifact-probe"

#: The one job with no rule. It is what makes a run on another ref a success.
GUARD: Final = "guard"

#: The one job that holds the identity token.
SIGN: Final = "sign"

#: The one ref that the probe measures on, and the rule of each measuring
#: job, as `jobs.<job>.if` spells it.
MAIN_REF: Final = "refs/heads/main"
ON_MAIN: Final = f"github.ref == '{MAIN_REF}'"

#: The jobs, in the order of the run.
JOB_NAMES: Final = ["guard", "build", "sign", "verify"]

#: The constants of the workflow. To take another cosign, compute the SHA-256
#: of the asset `cosign-linux-amd64` of the new release and compare it with
#: the file `cosign_checksums.txt` of that release. Then change the two
#: values here and in the workflow file.
PROBE_ENV: Final = {
    "PROBE_TARGET": "x86_64-unknown-linux-musl",
    "COSIGN_VERSION": "v3.1.3",
    "COSIGN_SHA256": "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
}
TARGET: Final = PROBE_ENV["PROBE_TARGET"]

#: The step that gives a job cosign.
COSIGN_STEP: Final = "cosign"

#: The whole text of that step. `set -euo pipefail` stops the step at the
#: first command that fails. The compare is before the line that makes the
#: file a program, and that line is before the first run of it. A pin of some
#: lines only lets a second fetch in between them.
COSIGN_RUN: Final = """\
set -euo pipefail
cosign="$RUNNER_TEMP/cosign"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \\
  --output "$cosign" \\
  "https://github.com/sigstore/cosign/releases/download/$COSIGN_VERSION/cosign-linux-amd64"
found="$(sha256sum "$cosign" | cut -d ' ' -f 1)"
if [[ "$found" != "$COSIGN_SHA256" ]]; then
  echo "cosign: the program has the SHA-256 $found, not $COSIGN_SHA256" >&2
  exit 1
fi
chmod 0755 "$cosign"
"$cosign" version
echo "COSIGN=$cosign" >> "$GITHUB_ENV"
"""

#: Each key of the step. One more key can change what a failure of the step
#: does, for example `continue-on-error` or `shell`.
COSIGN_KEYS: Final = {"name", "run"}

#: The three lines of that text that must keep their order.
COSIGN_COMPARE: Final = 'if [[ "$found" != "$COSIGN_SHA256" ]]; then'
COSIGN_MODE: Final = 'chmod 0755 "$cosign"'
COSIGN_FIRST_USE: Final = '"$cosign" version'

#: The whole text of the step that signs.
SIGN_RUN: Final = """\
set -euo pipefail
"$COSIGN" sign-blob --yes --bundle "$ARCHIVE.sigstore.json" "$ARCHIVE"
"""

#: The line that compares the archive with the job output of `build`.
DIGEST_CHECK: Final = 'sha256sum --check --strict <<< "$DIGEST"'

#: The whole text of the step that compares. `dotglob`: a name that starts
#: with a period is a second file too. The name of the archive starts with a
#: letter or a digit, so cosign cannot read it as an option.
DIGEST_RUN: Final = """\
set -euo pipefail
shopt -s dotglob
line='^[0-9a-f]{64}  [A-Za-z0-9][A-Za-z0-9._-]*$'
if [[ ! "$DIGEST" =~ $line ]]; then
  echo "sign: the job build gave no SHA-256 line" >&2
  exit 1
fi
archive="${DIGEST:66}"
for file in *; do
  if [[ "$file" != "$archive" ]]; then
    echo "sign: the job build did not name the file $file" >&2
    exit 1
  fi
done
sha256sum --check --strict <<< "$DIGEST"
echo "ARCHIVE=$archive" >> "$GITHUB_ENV"
"""

#: The directory of the job `sign` that holds the archive and its bundle.
SIGN_DIRECTORY: Final = "probe"

#: The machine of each job.
RUNNER: Final = "ubuntu-latest"

#: Each key of each job. One more key can change where a job runs or what
#: a failure of it does, for example `continue-on-error`.
JOB_KEYS: Final = {
    "guard": {"runs-on", "steps"},
    "build": {"needs", "if", "runs-on", "timeout-minutes", "outputs", "steps"},
    "sign": {"needs", "if", "runs-on", "timeout-minutes", "permissions", "env", "steps"},
    "verify": {"needs", "if", "runs-on", "timeout-minutes", "steps"},
}

#: What the job `build` hands to the two later jobs.
BUILD_OUTPUTS: Final = {
    "digest": "${{ steps.build.outputs.digest }}",
    "toolchain": "${{ steps.build.outputs.toolchain }}",
    "remap_needed": "${{ steps.build.outputs.remap_needed }}",
}

#: What the step `verify` reads from the two earlier jobs.
VERIFY_ENV: Final = {
    "PROBE_SIGNED": "${{ steps.signed.outputs.download-path }}",
    "PROBE_TOOLCHAIN": "${{ needs.build.outputs.toolchain }}",
    "PROBE_REMAP_NEEDED": "${{ needs.build.outputs.remap_needed }}",
}

#: Each action of the file. `test_gate_workflow.py` holds each one to a
#: commit id with its tag beside it.
CHECKOUT: Final = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
UPLOAD: Final = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD: Final = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"

#: The steps of each job that is no `run` text, in their order.
ACTIONS: Final = {
    "guard": [],
    "build": [CHECKOUT, UPLOAD],
    "sign": [DOWNLOAD, UPLOAD],
    "verify": [CHECKOUT, DOWNLOAD, UPLOAD],
}

#: Each workflow artifact: the job that writes it and the days that it stays.
ARTIFACTS: Final = {
    "probe-archive": ("build", 1),
    "probe-signed": ("sign", 1),
    "probe-report": ("verify", 90),
}

#: What no step and no line of the script can hold. The probe makes no tag
#: and no Release, and it changes no file of the repository.
FORBIDDEN: Final = ("gh release", "git tag", "git push")

#: The two commands of the script, as its header and the workflow give them.
BUILD_RUN: Final = "bin/artifact-probe.sh build"
VERIFY_RUN: Final = "bin/artifact-probe.sh verify"

#: The unit of the release executor, and the keys of its `[Service]` section
#: that are no sandbox line.
HANDOVER_UNIT: Final = REPO / "systemd" / "creche-handover.service"
NOT_A_SANDBOX_LINE: Final = {"Type", "ExecStart", "TimeoutStartSec", "Environment"}

#: The words that start each child, as the release executor will write them.
#: No `--pipe`: root reads the exit status of a child and no text.
RUN_WORDS: Final = [
    "/usr/bin/systemd-run",
    "--quiet",
    "--wait",
    "--collect",
    *("-p", "DynamicUser=yes"),
    *("-p", "StateDirectory=creche-verify"),
    *("-p", "NoNewPrivileges=yes"),
    *("-p", "ProtectSystem=strict"),
    *("-p", "ProtectHome=yes"),
    *("-p", "PrivateTmp=yes"),
    *("-p", "PrivateDevices=yes"),
    *("-p", "CapabilityBoundingSet="),
    *("-p", "MemoryMax=512M"),
    *("-p", "RuntimeMaxSec=120"),
    "--setenv=HOME=/var/lib/creche-verify",
    "--setenv=TUF_ROOT=/var/lib/creche-verify/tuf",
]
NO_NETWORK: Final = ["-p", "PrivateNetwork=yes"]

#: What a run of the script reads from the runner. The owner is an example.
REPOSITORY: Final = "example/agent-control"
SHA: Final = "5f8e4724c0ffee00000000000000000000000000"
ZERO_SHA: Final = "0" * 40

VERIFIER: Final = "/usr/local/bin/cosign"
COPY: Final = (
    f"/var/lib/creche-handover/work/PROBE/artifacts/agent-family/agent-family-{TARGET}.tar.gz"
)
BUNDLE: Final = f"{COPY}.sigstore.json"
TRUSTED_ROOT: Final = "/var/lib/creche-verify/tuf/example/targets/trusted_root.json"
IDENTITY: Final = (
    f"https://github.com/{REPOSITORY}/.github/workflows/artifact-probe.yml@refs/heads/main"
)

STUB_MODE: Final = 0o755

#: Whether this is a run of CI. A runner sets `CI`, and `bin/rust-gate.sh`
#: reads the variable the same way. A case that needs a tool of a runner
#: skips on a development machine that does not have the tool. In CI it runs,
#: so a runner that lost the tool fails the gate and does not pass with a
#: case that nothing ran.
IN_CI: Final = bool(os.environ.get("CI"))

#: The separator of the words of one logged call.
UNIT_SEPARATOR: Final = "\x1f"

#: The text that a stub child prints. No report can hold it.
CHILD_TEXT: Final = "TEXT-OF-A-CHILD"

#: `sudo`, and each program that runs behind it. It writes the words of each
#: call to STUB_LOG. A call of `systemd-run` ends with the status that the
#: test gives for its case, and each other call does nothing.
SUDO_STUB: Final = f"""#!/usr/bin/env bash
{{
  printf '%s\\037' sudo "$@"
  printf '\\n'
}} >> "$STUB_LOG"

case "$1" in
  find)
    printf '%s' "${{STUB_ROOTS:-}}"
    exit 0
    ;;
  dd)
    cat > /dev/null
    exit 0
    ;;
  cmp)
    exit "${{STUB_CMP:-1}}"
    ;;
  journalctl)
    echo "{CHILD_TEXT} from the journal"
    exit 0
    ;;
  /usr/bin/systemd-run) ;;
  *)
    exit 0
    ;;
esac

echo "{CHILD_TEXT}"
echo "{CHILD_TEXT}" >&2
words=" $* "
case "$words" in
  *" LockPersonality=yes "*) name=nested default=0 ;;
  *" {ZERO_SHA} "*) name=zero_sha default=1 ;;
  *" refs/heads/other "*) name=other_ref default=1 ;;
  *"/release.yml@"*) name=other_workflow default=1 ;;
  *".changed "*) name=changed_byte default=1 ;;
  *" initialize "*) name=refresh default=0 ;;
  *" /usr/bin/sleep "*) name=time_limit default=1 ;;
  *"/cosign-absent "*) name=absent_program default=203 ;;
  *" PrivateNetwork=yes "*) name=two_children default=0 ;;
  *) name=one_child default=0 ;;
esac
status="STUB_STATUS_$name"
exit "${{!status:-$default}}"
"""

#: The cosign program of the job: only its version line is read.
COSIGN_STUB: Final = """#!/bin/sh
echo "  ______   ______"
echo "GitVersion:    v9.9.9"
echo "GitCommit:     unknown"
"""

SYSTEMCTL_STUB: Final = """#!/bin/sh
echo "systemd 999 (999.9-stub)"
echo "+PAM +AUDIT"
"""

#: `rustup`: it writes its directory and its words to STUB_LOG, and it names
#: one toolchain.
TOOLCHAIN: Final = "1.92.0-x86_64-unknown-linux-gnu"
RUSTUP_STUB: Final = f"""#!/usr/bin/env bash
printf '%s\\037' rustup "$PWD" "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
if [[ "$1 $2" == "show active-toolchain" ]]; then
  echo "{TOOLCHAIN} (overridden by '$PWD/rust-toolchain.toml')"
fi
"""

#: `cargo`: it writes its directory, its words and the build variables to
#: STUB_LOG. Then it makes one program in the install root. The program holds
#: STUB_REMAP_TEXT in a build with the remap flags, and STUB_PLAIN_TEXT in a
#: build without them. With STUB_RECORD it also leaves the record file that
#: cargo writes without `--no-track`.
CARGO_STUB: Final = """#!/usr/bin/env bash
{
  printf '%s\\037' cargo "$PWD" "$@"
  printf 'CARGO_INSTALL_ROOT=%s\\037' "${CARGO_INSTALL_ROOT:-}"
  printf 'CARGO_BUILD_TARGET=%s\\037' "${CARGO_BUILD_TARGET:-}"
  printf 'RUSTUP_TOOLCHAIN=%s\\037' "${RUSTUP_TOOLCHAIN:-}"
  printf 'CARGO_INCREMENTAL=%s\\037' "${CARGO_INCREMENTAL:-}"
  printf 'RUSTFLAGS=%s\\037' "${RUSTFLAGS-<unset>}"
  printf '\\n'
} >> "$STUB_LOG"

if [[ -n "${RUSTFLAGS+set}" ]]; then
  text="${STUB_REMAP_TEXT:-}"
else
  text="${STUB_PLAIN_TEXT:-}"
  if [[ -n "${STUB_PLAIN_FAILS:-}" ]]; then
    exit 101
  fi
fi

if [[ -n "${STUB_NO_PROGRAM:-}" ]]; then
  exit 0
fi

mkdir -p "$CARGO_INSTALL_ROOT/bin"
if [[ -n "${STUB_RECORD:-}" ]]; then
  touch "$CARGO_INSTALL_ROOT/.crates.toml"
fi
printf '#!/bin/sh\\n# %s\\nexit 0\\n' "$text" > "$CARGO_INSTALL_ROOT/bin/agent-family"
chmod 0755 "$CARGO_INSTALL_ROOT/bin/agent-family"
"""

#: What `file` says for a static program of the target, and for a program
#: that needs a library of the host.
STATIC: Final = "ELF 64-bit LSB pie executable, x86-64, version 1 (SYSV), static-pie linked"
DYNAMIC: Final = "ELF 64-bit LSB pie executable, x86-64, version 1 (SYSV), dynamically linked"
FILE_STUB: Final = """#!/bin/sh
echo "${STUB_FILE}"
"""


def _steps(job: str) -> list[dict[str, Any]]:
    return JOBS[job]["steps"]


def _runs(job: str) -> list[str]:
    return [step["run"] for step in _steps(job) if "run" in step]


def _named(job: str, name: str) -> dict[str, Any]:
    (step,) = [one for one in _steps(job) if one.get("name") == name]

    return step


def _needs(job: str) -> list[str]:
    needs = JOBS[job].get("needs", [])

    return [needs] if isinstance(needs, str) else list(needs)


def test_the_workflow_has_the_name_that_a_certificate_carries() -> None:
    """The check of a signature names the workflow file. A file with another
    name signs with an identity that the script refuses."""
    assert PROBE["name"] == PROBE_NAME
    assert WORKFLOW.stem == PROBE_NAME
    assert f'WORKFLOW_FILE="{WORKFLOW.name}"' in SCRIPT_TEXT
    assert list(JOBS) == JOB_NAMES


def test_only_a_person_or_an_agent_starts_the_probe() -> None:
    """One trigger. A `pull_request` or a `push` trigger starts a run on the
    head of a pull request, and that run counts for its release. PyYAML reads
    the key `on` as the boolean."""
    assert PROBE[True] == {"workflow_dispatch": None}


def test_a_run_on_another_ref_measures_nothing_and_is_a_success() -> None:
    """`guard` has no rule, so each run has one job that passes. Each other
    job has the rule and needs `guard`, so it is skipped on another ref."""
    assert "if" not in JOBS[GUARD]
    assert "needs" not in JOBS[GUARD]
    assert f'MAIN_REF="{MAIN_REF}"' in SCRIPT_TEXT

    for name in JOB_NAMES:
        if name == GUARD:
            continue

        assert JOBS[name]["if"] == ON_MAIN, f"{name} also runs on another ref"
        assert GUARD in _needs(name), f"{name} does not need {GUARD}"


def test_the_jobs_run_one_after_the_other() -> None:
    assert _needs("build") == [GUARD]
    assert _needs(SIGN) == [GUARD, "build"]
    assert _needs("verify") == [GUARD, "build", SIGN]


@pytest.mark.parametrize("job", JOB_NAMES)
def test_each_job_has_its_keys_and_no_other(job: str) -> None:
    assert set(JOBS[job]) == JOB_KEYS[job]
    assert JOBS[job]["runs-on"] == RUNNER


def test_only_the_sign_job_holds_the_identity_token_and_nothing_else() -> None:
    assert PROBE["permissions"] == {"contents": "read"}
    assert JOBS[SIGN]["permissions"] == {"id-token": "write"}

    for name, job in JOBS.items():
        if name != SIGN:
            assert "permissions" not in job, f"{name} sets its own permissions"


def test_the_sign_job_runs_no_code_of_the_repository() -> None:
    """The job that can sign has no checkout and no local action, and no step
    of it starts a file of the repository. A change of the repository then
    cannot run beside the identity token."""
    for step in _steps(SIGN):
        uses = step.get("uses", "")

        assert not uses.startswith("actions/checkout@"), "the sign job has a checkout"
        assert not uses.startswith("./"), f"the sign job uses the local action {uses}"

    for run in _runs(SIGN):
        assert "bin/" not in run, "a step of the sign job starts a script of the repository"
        assert "${{" not in run, "a step of the sign job holds an expression in its text"


def test_the_sign_job_has_five_steps_and_no_other() -> None:
    """The job that holds the identity token is pinned as a whole: the order
    of its steps, the whole text of each `run` step, each key of the step
    that compares, and the inputs of the two actions. A new step, a key that
    lets a failed step pass, or another directory then fails here."""
    download, digest, _, _, upload = _steps(SIGN)

    assert [step.get("name") for step in _steps(SIGN)] == [None, "digest", COSIGN_STEP, SIGN, None]
    assert _runs(SIGN) == [DIGEST_RUN, COSIGN_RUN, SIGN_RUN]
    assert download == {
        "uses": DOWNLOAD,
        "with": {"name": "probe-archive", "path": SIGN_DIRECTORY},
    }
    assert set(digest) == {"name", "working-directory", "run"}
    assert digest["working-directory"] == SIGN_DIRECTORY
    assert upload == {
        "uses": UPLOAD,
        "with": {
            "name": "probe-signed",
            "path": SIGN_DIRECTORY,
            "if-no-files-found": "error",
            "retention-days": 1,
        },
    }


@pytest.mark.parametrize("job", JOB_NAMES)
def test_each_job_uses_the_pinned_actions_and_no_other(job: str) -> None:
    uses = [step["uses"] for step in _steps(job) if "uses" in step]

    assert uses == ACTIONS[job]


def test_no_checkout_leaves_a_credential_in_the_tree() -> None:
    """A build script runs in the job `build`. With `persist-credentials` the
    token of the job stays in the git config of the checkout."""
    checkouts = [
        step for name in JOB_NAMES for step in _steps(name) if step.get("uses") == CHECKOUT
    ]

    assert len(checkouts) == 2
    for step in checkouts:
        assert step["with"] == {"persist-credentials": False}


def test_the_constants_of_the_workflow_have_one_place() -> None:
    """The target and the cosign pin are in the top-level `env`. No job and
    no step sets them again."""
    assert PROBE["env"] == PROBE_ENV

    for name in JOB_NAMES:
        holders = [JOBS[name], *_steps(name)]
        for holder in holders:
            assert not set(holder.get("env", {})) & set(PROBE_ENV), f"{name} sets a constant again"


@pytest.mark.parametrize("job", [SIGN, "verify"])
def test_a_job_takes_cosign_from_a_file_that_it_checked(job: str) -> None:
    """No action installs cosign, so no commit pin holds the program. The
    SHA-256 of the file is the pin. The test holds the whole text of the step
    and each key of the step, and the two jobs hold the same step."""
    step = _named(job, COSIGN_STEP)

    assert set(step) == COSIGN_KEYS
    assert step["run"] == COSIGN_RUN
    assert step == _named(SIGN, COSIGN_STEP)


def test_the_compare_is_before_the_first_use_of_cosign() -> None:
    assert (
        COSIGN_RUN.index(COSIGN_COMPARE)
        < COSIGN_RUN.index(COSIGN_MODE)
        < COSIGN_RUN.index(COSIGN_FIRST_USE)
    )


@pytest.mark.parametrize("job", [SIGN, "verify"])
def test_no_step_before_the_cosign_step_uses_the_program(job: str) -> None:
    """The step that uses cosign has the name of its job."""
    names = [step.get("name") for step in _steps(job)]
    before = _steps(job)[: names.index(COSIGN_STEP)]

    assert names.index(COSIGN_STEP) < names.index(job)
    for step in before:
        assert "cosign" not in step.get("run", "").lower()


def test_the_build_job_hands_the_archive_and_its_digest_on() -> None:
    build = JOBS["build"]
    step = _named("build", "build")
    upload = _steps("build")[-1]

    assert _runs("build") == [BUILD_RUN]
    assert set(step) == {"name", "id", "run"}
    assert build["outputs"] == BUILD_OUTPUTS
    assert upload["with"]["path"] == "${{ steps.build.outputs.archive }}"


def test_the_sign_job_signs_only_the_archive_that_the_build_job_named() -> None:
    """Each job of a run can write a workflow artifact. The job compares the
    file with the SHA-256 of the job output before it takes cosign, and it
    signs the file of that line."""
    names = [step.get("name") for step in _steps(SIGN)]
    digest = _named(SIGN, "digest")
    sign = _named(SIGN, SIGN)

    assert JOBS[SIGN]["env"] == {"DIGEST": "${{ needs.build.outputs.digest }}"}
    assert names.index("digest") < names.index(COSIGN_STEP) < names.index(SIGN)
    assert DIGEST_CHECK in digest["run"].splitlines()[-2].strip()
    assert digest["run"].splitlines()[-1].strip() == 'echo "ARCHIVE=$archive" >> "$GITHUB_ENV"'
    assert digest["working-directory"] == sign["working-directory"]
    assert set(sign) == {"name", "working-directory", "run"}
    assert sign["run"] == SIGN_RUN


#: An archive for the step that compares, and its line of `sha256sum`.
ARCHIVE: Final = f"agent-family-{TARGET}.tar.gz"
ARCHIVE_BYTES: Final = b"the bytes of an archive"
ARCHIVE_LINE: Final = f"{hashlib.sha256(ARCHIVE_BYTES).hexdigest()}  {ARCHIVE}"

#: A file name that a program reads as its options.
OPTION_NAME: Final = "-rf"


def _sha256sum_checks_a_line() -> bool:
    """Whether the `sha256sum` of this machine takes `--check --strict` and a
    line on its standard input, as the one of a runner does."""
    program = shutil.which("sha256sum")
    if program is None:
        return False

    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / ARCHIVE).write_bytes(ARCHIVE_BYTES)
        done = subprocess.run(
            [program, "--check", "--strict"],
            cwd=directory,
            input=f"{ARCHIVE_LINE}\n",
            capture_output=True,
            text=True,
            check=False,
        )

    return done.returncode == 0


needs_sha256sum_check = pytest.mark.skipif(
    not IN_CI and not _sha256sum_checks_a_line(),
    reason="the step uses `sha256sum --check --strict`",
)
needs_sha256sum = pytest.mark.skipif(
    not IN_CI and shutil.which("sha256sum") is None, reason="the step uses sha256sum"
)


def _digest_step(
    tmp_path: Path, line: str, files: dict[str, bytes]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Runs the text of the step `digest` in a directory that holds `files`,
    with `line` as the job output of `build`. Returns the run and what the
    step wrote for the next step."""
    probe = tmp_path / "probe"
    probe.mkdir()
    for name, body in files.items():
        (probe / name).write_bytes(body)

    handed = tmp_path / "github-env"
    handed.touch()
    done = subprocess.run(
        ["bash", "-c", _named(SIGN, "digest")["run"]],
        cwd=probe,
        env={"PATH": os.environ["PATH"], "DIGEST": line, "GITHUB_ENV": str(handed)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    return done, handed.read_text(encoding="utf-8")


@needs_sha256sum_check
def test_the_digest_step_names_the_archive_that_it_compared(tmp_path: Path) -> None:
    done, handed = _digest_step(tmp_path, ARCHIVE_LINE, {ARCHIVE: ARCHIVE_BYTES})

    assert done.returncode == 0, done.stdout + done.stderr
    assert handed == f"ARCHIVE={ARCHIVE}\n"


@needs_sha256sum_check
def test_the_digest_step_refuses_an_archive_with_other_bytes(tmp_path: Path) -> None:
    done, handed = _digest_step(tmp_path, ARCHIVE_LINE, {ARCHIVE: ARCHIVE_BYTES + b"!"})

    assert done.returncode != 0
    assert handed == ""


@pytest.mark.parametrize("second", ["second.tar.gz", ".hidden"])
def test_the_digest_step_refuses_a_file_that_the_build_job_did_not_name(
    tmp_path: Path, second: str
) -> None:
    """A plain `*` does not match a name that starts with a period. The step
    sets `dotglob`, so such a file is a second file too."""
    files = {ARCHIVE: ARCHIVE_BYTES, second: b""}
    done, handed = _digest_step(tmp_path, ARCHIVE_LINE, files)

    assert done.returncode == 1
    assert f"did not name the file {second}" in done.stderr
    assert handed == ""


def test_the_digest_step_refuses_a_directory_with_no_file(tmp_path: Path) -> None:
    done, handed = _digest_step(tmp_path, ARCHIVE_LINE, {})

    assert done.returncode == 1
    assert "did not name the file" in done.stderr
    assert handed == ""


@pytest.mark.parametrize(
    "line",
    [
        "",
        ARCHIVE,
        ARCHIVE_LINE.replace("  ", " "),
        ARCHIVE_LINE.upper(),
        ARCHIVE_LINE.replace("  ", "  ../"),
        f"{ARCHIVE_LINE} second.tar.gz",
        ARCHIVE_LINE.replace(ARCHIVE, OPTION_NAME),
        ARCHIVE_LINE.replace(ARCHIVE, f".{ARCHIVE}"),
    ],
    ids=[
        "empty",
        "no-digest",
        "one-space",
        "upper-case",
        "a-directory",
        "two-names",
        "an-option",
        "a-hidden-name",
    ],
)
def test_the_digest_step_refuses_a_job_output_that_is_no_sha256_line(
    tmp_path: Path, line: str
) -> None:
    """The file of the line exists in each case, so only the line itself can
    stop the step."""
    files = {ARCHIVE: ARCHIVE_BYTES, OPTION_NAME: ARCHIVE_BYTES, f".{ARCHIVE}": ARCHIVE_BYTES}
    done, handed = _digest_step(tmp_path, line, files)

    assert done.returncode == 1
    assert "gave no SHA-256 line" in done.stderr
    assert handed == ""


#: `curl`: it writes its words to STUB_LOG and puts STUB_PROGRAM at the path
#: after `--output`.
CURL_STUB: Final = """#!/usr/bin/env bash
printf '%s\\037' curl "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
while [[ "$#" -gt 0 ]]; do
  if [[ "$1" == "--output" ]]; then
    cp "$STUB_PROGRAM" "$2"
  fi
  shift
done
"""

#: What the stub `curl` fetches: a program that writes its words to STUB_USED.
FETCHED: Final = """#!/bin/sh
echo "$*" >> "$STUB_USED"
"""


class CosignStep:
    """One run of the text of the step `cosign`. `sha256` is the constant
    that the step compares with."""

    def __init__(self, tmp_path: Path, sha256: str) -> None:
        stubs = tmp_path / "stubs"
        _stub(stubs, "curl", CURL_STUB)
        fetched = tmp_path / "fetched"
        fetched.write_text(FETCHED, encoding="utf-8")

        self.program = tmp_path / "temp" / "cosign"
        self.program.parent.mkdir()
        self.log = tmp_path / "calls.log"
        self.used = tmp_path / "used"
        self.handed = tmp_path / "github-env"
        self.handed.touch()
        self.done = subprocess.run(
            ["bash", "-c", COSIGN_RUN],
            env={
                "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
                "RUNNER_TEMP": str(self.program.parent),
                "GITHUB_ENV": str(self.handed),
                "COSIGN_VERSION": PROBE_ENV["COSIGN_VERSION"],
                "COSIGN_SHA256": sha256,
                "STUB_LOG": str(self.log),
                "STUB_PROGRAM": str(fetched),
                "STUB_USED": str(self.used),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )


@needs_sha256sum
def test_the_cosign_step_runs_the_program_only_after_the_compare(tmp_path: Path) -> None:
    """The fetched file has the SHA-256 of the constant. The step then makes
    it a program, runs it one time and names it for the next step."""
    step = CosignStep(tmp_path, hashlib.sha256(FETCHED.encode()).hexdigest())

    assert step.done.returncode == 0, step.done.stdout + step.done.stderr
    assert _calls(step.log, "curl") == [
        [
            "curl",
            *("--proto", "=https"),
            "--tlsv1.2",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            *("--retry", "3"),
            *("--output", str(step.program)),
            "https://github.com/sigstore/cosign/releases/download/v3.1.3/cosign-linux-amd64",
        ]
    ]
    assert step.used.read_text(encoding="utf-8") == "version\n"
    assert step.handed.read_text(encoding="utf-8") == f"COSIGN={step.program}\n"


@needs_sha256sum
def test_the_cosign_step_never_runs_a_file_with_another_sha256(tmp_path: Path) -> None:
    """The fetched file does not have the SHA-256 of the pin. The step stops:
    the file is no program, nothing ran it and no later step gets its path."""
    step = CosignStep(tmp_path, PROBE_ENV["COSIGN_SHA256"])

    assert step.done.returncode == 1
    assert f"not {PROBE_ENV['COSIGN_SHA256']}" in step.done.stderr
    assert not step.used.exists()
    assert not os.access(step.program, os.X_OK)
    assert step.handed.read_text(encoding="utf-8") == ""


def test_the_verify_job_runs_the_script_on_the_signed_files() -> None:
    step = _named("verify", "verify")

    assert _runs("verify") == [COSIGN_RUN, VERIFY_RUN]
    assert set(step) == {"name", "id", "env", "run"}
    assert step["env"] == VERIFY_ENV
    assert _steps("verify")[1]["id"] == "signed"


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_each_workflow_artifact_has_one_writer_and_a_time_limit(artifact: str) -> None:
    job, days = ARTIFACTS[artifact]
    uploads = [
        (name, step)
        for name in JOB_NAMES
        for step in _steps(name)
        if step.get("uses") == UPLOAD and step["with"]["name"] == artifact
    ]
    ((writer, step),) = uploads

    assert writer == job
    assert step["with"]["retention-days"] == days
    assert step["with"]["if-no-files-found"] == "error"


def test_each_download_takes_the_artifact_of_the_job_before() -> None:
    taken = {
        name: step["with"]["name"]
        for name in JOB_NAMES
        for step in _steps(name)
        if step.get("uses") == DOWNLOAD
    }

    assert taken == {SIGN: "probe-archive", "verify": "probe-signed"}


def test_a_red_verify_job_still_gives_its_report() -> None:
    """The report says which case failed. No other step has a rule of its
    own: a failed step stops its job."""
    with_a_rule = [(name, step) for name in JOB_NAMES for step in _steps(name) if "if" in step]
    ((job, step),) = with_a_rule

    assert job == "verify"
    assert step["with"]["name"] == "probe-report"
    assert step["if"] == "${{ !cancelled() && steps.verify.outputs.report != '' }}"


def test_the_probe_makes_no_tag_no_release_and_no_push() -> None:
    texts = [run for name in JOB_NAMES for run in _runs(name)]

    for text in [*texts, SCRIPT_TEXT]:
        for words in FORBIDDEN:
            assert words not in text


def test_the_header_of_the_script_says_who_runs_it() -> None:
    """`bin/AGENTS.md`: `ROOT`, `OPERATOR` or `CI`, in the first three lines,
    with the exact command."""
    head = SCRIPT_TEXT.splitlines()[:3]
    header = SCRIPT_TEXT.split("set -uo pipefail", 1)[0]

    assert any(line.startswith("# CI:") for line in head)
    assert BUILD_RUN in header
    assert VERIFY_RUN in header


def test_no_child_hands_its_text_to_the_script() -> None:
    code = [line for line in SCRIPT_TEXT.splitlines() if not line.lstrip().startswith("#")]

    assert all("--pipe" not in line for line in code)


def _sandbox_lines() -> list[str]:
    """Each sandbox line of the release executor, in the order of its unit
    file: `Key=value`."""
    service = HANDOVER_UNIT.read_text(encoding="utf-8").split("[Service]", 1)[1]
    lines = [line for line in service.splitlines() if "=" in line and not line.startswith("#")]

    return [line for line in lines if line.split("=", 1)[0] not in NOT_A_SANDBOX_LINE]


def test_the_script_knows_each_sandbox_line_of_the_executor_unit() -> None:
    """The nested case starts a unit with each sandbox line of the release
    executor. A line that the unit gets later must reach the probe too."""
    keys = [line.split("=", 1)[0] for line in _sandbox_lines()]
    found = re.search(r"^SANDBOX_LINES=\(\n(.*?)^\)$", SCRIPT_TEXT, re.MULTILINE | re.DOTALL)
    assert found is not None

    assert found.group(1).split() == keys
    assert len(keys) == len(set(keys)) == 11


def _stub(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(exist_ok=True)
    stub = directory / name
    stub.write_text(text, encoding="utf-8")
    stub.chmod(STUB_MODE)

    return stub


def _calls(log: Path, first: str) -> list[list[str]]:
    """The words of each logged call that starts with `first`."""
    if not log.exists():
        return []

    lines = log.read_text(encoding="utf-8").splitlines()
    calls = [line.split(UNIT_SEPARATOR)[:-1] for line in lines]

    return [call for call in calls if call[0] == first]


class Verify:
    """One run of `bin/artifact-probe.sh verify` against the stubs."""

    def __init__(self, tmp_path: Path, **overrides: str) -> None:
        stubs = tmp_path / "stubs"
        _stub(stubs, "sudo", SUDO_STUB)
        _stub(stubs, "systemctl", SYSTEMCTL_STUB)
        cosign = _stub(tmp_path / "temp", "cosign", COSIGN_STUB)
        signed = tmp_path / "signed"
        signed.mkdir()
        (signed / f"agent-family-{TARGET}.tar.gz").write_bytes(b"\x1f\x8b archive")
        (signed / f"agent-family-{TARGET}.tar.gz.sigstore.json").write_bytes(b"{}")

        self.log = tmp_path / "calls.log"
        self.summary = tmp_path / "summary.md"
        self.output = tmp_path / "output"
        self.report_path = tmp_path / "temp" / "probe-report.json"
        env = {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path / "home"),
            "GITHUB_WORKSPACE": str(REPO),
            "RUNNER_TEMP": str(tmp_path / "temp"),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_SHA": SHA,
            "PROBE_TARGET": TARGET,
            "PROBE_SIGNED": str(signed),
            "PROBE_TOOLCHAIN": TOOLCHAIN,
            "PROBE_REMAP_NEEDED": "yes",
            "COSIGN": str(cosign),
            "STUB_LOG": str(self.log),
            "STUB_ROOTS": f"{TRUSTED_ROOT}\n",
        }
        self.done = subprocess.run(
            ["bash", str(SCRIPT), "verify"],
            env=env | overrides,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @property
    def said(self) -> str:
        return self.done.stdout + self.done.stderr

    @property
    def report(self) -> dict[str, Any]:
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    @property
    def children(self) -> list[list[str]]:
        """The words of each `systemd-run` call, without `sudo`."""
        return [call[1:] for call in _calls(self.log, "sudo") if call[1] == RUN_WORDS[0]]


needs_jq = pytest.mark.skipif(
    not IN_CI and shutil.which("jq") is None, reason="the script writes with jq"
)


def _check(
    *,
    root: str | None = TRUSTED_ROOT,
    workflow: str = "artifact-probe.yml",
    ref: str = "refs/heads/main",
    sha: str = SHA,
    blob: str = COPY,
) -> list[str]:
    """The program words of one check, as the release executor will write
    them."""
    trusted = [] if root is None else ["--trusted-root", root]
    identity = IDENTITY.replace("artifact-probe.yml", workflow)

    return [
        VERIFIER,
        "verify-blob",
        *("--bundle", BUNDLE),
        *trusted,
        *("--certificate-oidc-issuer", "https://token.actions.githubusercontent.com"),
        *("--certificate-identity", identity),
        *("--certificate-github-workflow-repository", REPOSITORY),
        *("--certificate-github-workflow-ref", ref),
        *("--certificate-github-workflow-sha", sha),
        blob,
    ]


@needs_jq
def test_verify_starts_each_child_with_the_words_of_the_design(tmp_path: Path) -> None:
    run = Verify(tmp_path)
    offline = [*RUN_WORDS, *NO_NETWORK, "--"]
    sandbox = [word for line in _sandbox_lines() for word in ("-p", line)]

    assert run.done.returncode == 0, run.said
    assert run.children == [
        [*RUN_WORDS, "--", *_check(root=None)],
        [*RUN_WORDS, "--", VERIFIER, "initialize"],
        [*offline, *_check()],
        [*offline, *_check(sha=ZERO_SHA)],
        [*offline, *_check(ref="refs/heads/other")],
        [*offline, *_check(workflow="release.yml")],
        [*offline, *_check(blob=f"{COPY}.changed")],
        [*RUN_WORDS, "--", "/usr/local/bin/cosign-absent"],
        [*RUN_WORDS, "-p", "RuntimeMaxSec=1", "--", "/usr/bin/sleep", "5"],
        [*RUN_WORDS[:4], *sandbox, "--", *offline, *_check()],
    ]
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in sandbox
    for call in _calls(run.log, "sudo"):
        assert "--pipe" not in call


@needs_jq
def test_verify_puts_the_verifier_and_the_two_files_where_a_dynamic_user_reads_them(
    tmp_path: Path,
) -> None:
    run = Verify(tmp_path)
    signed = tmp_path / "signed" / f"agent-family-{TARGET}.tar.gz"
    owner = ["install", "-o", "root", "-g", "root"]
    installs = [call[1:] for call in _calls(run.log, "sudo") if call[1] == "install"]

    assert installs == [
        [*owner, "-m", "0755", "--", str(tmp_path / "temp" / "cosign"), VERIFIER],
        ["install", "-d", "-o", "root", "-g", "root", "-m", "0755", "--", str(Path(COPY).parent)],
        [*owner, "-m", "0644", "--", str(signed), COPY],
        [*owner, "-m", "0644", "--", f"{signed}.sigstore.json", BUNDLE],
        [*owner, "-m", "0644", "--", str(signed), f"{COPY}.changed"],
    ]


@needs_jq
def test_the_report_is_one_strict_json_text_with_one_key_for_each_result(
    tmp_path: Path,
) -> None:
    run = Verify(tmp_path)
    report = run.report
    seconds = {key: value for key, value in report.items() if key.startswith("seconds_")}
    rest = {key: value for key, value in report.items() if not key.startswith("seconds_")}

    assert run.done.returncode == 0, run.said
    assert rest == {
        "target": TARGET,
        "toolchain": TOOLCHAIN,
        "remap_needed": "yes",
        "two_children": "yes",
        "trusted_root_path": TRUSTED_ROOT,
        "trusted_root_count": 1,
        "verify_words": _check(),
        "status_one_child": 0,
        "status_refresh": 0,
        "status_two_children": 0,
        "status_zero_sha": 1,
        "status_other_ref": 1,
        "status_other_workflow": 1,
        "status_changed_byte": 1,
        "status_absent_program": 203,
        "status_time_limit": 1,
        "status_nested": 0,
        "systemd_version": "systemd 999 (999.9-stub)",
        "cosign_version": "v9.9.9",
        "archive_bytes": 10,
        "bundle_bytes": 2,
    }
    assert sorted(seconds) == sorted(
        key.replace("status_", "seconds_") for key in rest if key.startswith("status_")
    )
    for value in seconds.values():
        assert isinstance(value, float | int) and value >= 0

    # One line of the step summary for each key, and the path for the upload.
    summary = run.summary.read_text(encoding="utf-8")
    for key in report:
        assert f"\n- {key}: " in f"\n{summary}", f"the summary has no line for {key}"
    assert run.output.read_text(encoding="utf-8") == f"report={run.report_path}\n"


@needs_jq
def test_the_report_holds_no_text_of_a_child(tmp_path: Path) -> None:
    """Each stub child prints a text, and the journal stub prints one for the
    case that fails here. The log of the job can show it. The report cannot."""
    run = Verify(tmp_path, STUB_STATUS_two_children="1")

    assert CHILD_TEXT in run.done.stdout
    assert CHILD_TEXT not in run.report_path.read_text(encoding="utf-8")
    assert CHILD_TEXT not in run.summary.read_text(encoding="utf-8")


@needs_jq
def test_a_check_with_no_network_that_fails_leaves_the_way_with_one_child(
    tmp_path: Path,
) -> None:
    """The probe then reports `two_children: no` and passes. Each later case
    uses the words of the child that has the network."""
    run = Verify(tmp_path, STUB_STATUS_two_children="1")
    online = [*RUN_WORDS, "--"]

    assert run.done.returncode == 0, run.said
    assert run.report["two_children"] == "no"
    assert run.report["status_two_children"] == 1
    assert run.report["verify_words"] == _check(root=None)
    assert run.children[3:7] == [
        [*online, *_check(root=None, sha=ZERO_SHA)],
        [*online, *_check(root=None, ref="refs/heads/other")],
        [*online, *_check(root=None, workflow="release.yml")],
        [*online, *_check(root=None, blob=f"{COPY}.changed")],
    ]
    assert run.children[-1][-len(online) - len(_check(root=None)) :] == [
        *online,
        *_check(root=None),
    ]


@needs_jq
def test_with_no_trusted_root_file_no_second_child_starts(tmp_path: Path) -> None:
    run = Verify(tmp_path, STUB_ROOTS="")

    assert run.done.returncode == 0, run.said
    assert run.report["two_children"] == "no"
    assert run.report["trusted_root_path"] is None
    assert run.report["trusted_root_count"] == 0
    assert run.report["status_two_children"] is None
    assert run.report["seconds_two_children"] is None
    assert run.report["verify_words"] == _check(root=None)
    assert all(NO_NETWORK[1] not in call for call in run.children)


@needs_jq
def test_the_first_of_two_trusted_root_files_is_the_one_that_the_probe_uses(
    tmp_path: Path,
) -> None:
    second = TRUSTED_ROOT.replace("example", "other")
    run = Verify(tmp_path, STUB_ROOTS=f"{second}\n{TRUSTED_ROOT}\n")

    assert run.report["trusted_root_path"] == TRUSTED_ROOT
    assert run.report["trusted_root_count"] == 2


@needs_jq
@pytest.mark.parametrize("case", ["zero_sha", "other_ref", "other_workflow", "changed_byte"])
def test_a_check_that_must_fail_and_passes_fails_the_probe(tmp_path: Path, case: str) -> None:
    run = Verify(tmp_path, **{f"STUB_STATUS_{case}": "0"})

    assert run.done.returncode == 1, run.said
    assert run.report[f"status_{case}"] == 0
    assert "PROBE: FAIL (1)" in run.done.stdout


@needs_jq
@pytest.mark.parametrize("case", ["one_child", "refresh", "nested"])
def test_a_check_that_must_pass_and_fails_fails_the_probe(tmp_path: Path, case: str) -> None:
    run = Verify(tmp_path, **{f"STUB_STATUS_{case}": "1"})

    assert run.done.returncode == 1, run.said
    assert run.report[f"status_{case}"] == 1
    assert "PROBE: FAIL (1)" in run.done.stdout


@needs_jq
def test_with_no_check_that_passed_the_report_still_names_each_case(tmp_path: Path) -> None:
    """The cases that need a passing check do not run. The two fault classes
    need none, so they run."""
    run = Verify(tmp_path, STUB_STATUS_one_child="1", STUB_ROOTS="")
    report = run.report

    assert run.done.returncode == 1, run.said
    assert report["verify_words"] == []
    for case in ("zero_sha", "other_ref", "other_workflow", "changed_byte", "nested"):
        assert report[f"status_{case}"] is None
    assert report["status_absent_program"] == 203
    assert report["status_time_limit"] == 1
    assert len(run.children) == 4


@needs_jq
def test_a_copy_that_did_not_change_fails_the_probe(tmp_path: Path) -> None:
    """`cmp` says that the changed copy equals the archive. A check of that
    copy would pass for the wrong reason, so the case does not run."""
    run = Verify(tmp_path, STUB_CMP="0")

    assert run.done.returncode == 1, run.said
    assert run.report["status_changed_byte"] is None


@needs_jq
def test_the_two_fault_classes_are_recorded_and_fail_nothing(tmp_path: Path) -> None:
    run = Verify(tmp_path, STUB_STATUS_absent_program="1", STUB_STATUS_time_limit="143")

    assert run.done.returncode == 0, run.said
    assert run.report["status_absent_program"] == 1
    assert run.report["status_time_limit"] == 143


def test_verify_stops_before_the_first_child_without_the_signed_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    run = Verify(tmp_path, PROBE_SIGNED=str(empty))

    assert run.done.returncode == 1
    assert "gave no file" in run.done.stderr
    assert _calls(run.log, "sudo") == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_SHA", "main"),
        ("GITHUB_REPOSITORY", "example"),
        ("PROBE_TARGET", "x86_64 musl"),
        ("COSIGN", ""),
    ],
)
def test_verify_refuses_a_value_that_cannot_be_a_word_of_a_check(
    tmp_path: Path, name: str, value: str
) -> None:
    run = Verify(tmp_path, **{name: value})

    assert run.done.returncode == 1
    assert name in run.done.stderr
    assert _calls(run.log, "sudo") == []


def test_the_script_refuses_a_command_that_it_does_not_have(tmp_path: Path) -> None:
    done = subprocess.run(["bash", str(SCRIPT), "sign"], capture_output=True, text=True, timeout=60)

    assert done.returncode == 1
    assert "usage: bin/artifact-probe.sh build|verify" in done.stderr


def _has_gnu_tar() -> bool:
    tar = shutil.which("tar")
    if tar is None:
        return False

    done = subprocess.run([tar, "--version"], capture_output=True, text=True, check=False)

    return "GNU tar" in done.stdout


needs_gnu_tar = pytest.mark.skipif(
    not IN_CI and not _has_gnu_tar(), reason="the pack needs GNU tar"
)


class Build:
    """One run of `bin/artifact-probe.sh build` against the stubs."""

    def __init__(self, tmp_path: Path, **overrides: str) -> None:
        stubs = tmp_path / "stubs"
        _stub(stubs, "rustup", RUSTUP_STUB)
        _stub(stubs, "cargo", CARGO_STUB)
        _stub(stubs, "file", FILE_STUB)

        self.workspace = tmp_path / "work-space"
        (self.workspace / "rust").mkdir(parents=True)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.temp = tmp_path / "temp"
        self.temp.mkdir()
        self.log = tmp_path / "calls.log"
        self.summary = tmp_path / "summary.md"
        self.output = tmp_path / "output"
        env = {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "GITHUB_WORKSPACE": str(self.workspace),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "PROBE_TARGET": TARGET,
            "STUB_LOG": str(self.log),
            "STUB_FILE": STATIC,
        }
        self.done = subprocess.run(
            ["bash", str(SCRIPT), "build"],
            env=env | overrides,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @property
    def said(self) -> str:
        return self.done.stdout + self.done.stderr

    @property
    def outputs(self) -> dict[str, str]:
        if not self.output.exists():
            return {}

        lines = self.output.read_text(encoding="utf-8").splitlines()

        return dict(line.split("=", 1) for line in lines)

    @property
    def remap_flags(self) -> str:
        return (
            f"--remap-path-prefix={self.workspace}=/probe/workspace "
            f"--remap-path-prefix={self.home}/.cargo=/probe/cargo-home "
            f"--remap-path-prefix={self.temp}=/probe/temp"
        )


def test_a_tree_that_holds_a_path_of_the_runner_stops_the_build(tmp_path: Path) -> None:
    """The build with the remap flags left a path in the program. The job
    stops before the pack, so the job `sign` gets no archive."""
    home = tmp_path / "home"
    run = Build(tmp_path, STUB_REMAP_TEXT=f"{home}/.cargo/registry/src/one.rs")

    assert run.done.returncode == 1, run.said
    assert "bin/agent-family holds HOME" in run.done.stdout
    assert "bin/agent-family holds the cargo home" in run.done.stdout
    assert f"\n{home}/.cargo/registry/src/one.rs\n" in run.done.stdout
    assert "a file of the tree holds a path of the runner" in run.done.stderr
    assert "digest" not in run.outputs
    assert len(_calls(run.log, "cargo")) == 1


@pytest.mark.parametrize("variable", ["GITHUB_WORKSPACE", "RUNNER_TEMP"])
def test_each_path_of_the_runner_is_a_needle(tmp_path: Path, variable: str) -> None:
    paths = {
        "GITHUB_WORKSPACE": tmp_path / "work-space" / "rust" / "one.rs",
        "RUNNER_TEMP": tmp_path / "temp" / "one.rs",
    }
    run = Build(tmp_path, STUB_REMAP_TEXT=str(paths[variable]))

    assert run.done.returncode == 1, run.said
    assert f"bin/agent-family holds {variable}" in run.done.stdout


def test_a_program_that_needs_a_library_of_the_host_stops_the_build(tmp_path: Path) -> None:
    run = Build(tmp_path, STUB_FILE=DYNAMIC)

    assert run.done.returncode == 1, run.said
    assert "the program has no static link" in run.done.stderr
    assert "digest" not in run.outputs


def test_a_program_for_another_machine_stops_the_build(tmp_path: Path) -> None:
    run = Build(tmp_path, STUB_FILE="ELF 64-bit LSB executable, ARM aarch64, statically linked")

    assert run.done.returncode == 1, run.said
    assert "the program is not an x86-64 program" in run.done.stderr


def test_a_build_that_made_no_program_in_the_install_root_stops(tmp_path: Path) -> None:
    """cargo then did not read the install root or the target."""
    run = Build(tmp_path, STUB_NO_PROGRAM="1")

    assert run.done.returncode == 1, run.said
    assert "the build made no program at bin/agent-family" in run.done.stderr


def test_build_stops_for_a_path_that_rustflags_cannot_carry(tmp_path: Path) -> None:
    run = Build(tmp_path, RUNNER_TEMP=str(tmp_path / "a temp"))

    assert run.done.returncode == 1
    assert "RUSTFLAGS cannot carry it" in run.done.stderr
    assert _calls(run.log, "cargo") == []


def test_build_installs_the_toolchain_of_rust_and_builds_from_the_root(tmp_path: Path) -> None:
    """The words of each call, and the variables of each build. The pack
    comes after the two builds, so this case needs no GNU tar."""
    run = Build(tmp_path)
    rust = str(run.workspace / "rust")
    work = run.temp / "artifact-probe"
    words = ["install", "--locked", "--no-track", "--path", "rust/crates/agent-family"]
    shared = [
        f"CARGO_BUILD_TARGET={TARGET}",
        f"RUSTUP_TOOLCHAIN={TOOLCHAIN}",
        "CARGO_INCREMENTAL=0",
    ]

    assert _calls(run.log, "rustup") == [
        ["rustup", rust, "toolchain", "install", "--no-self-update"],
        ["rustup", rust, "show", "active-toolchain"],
        ["rustup", rust, "target", "add", "--toolchain", TOOLCHAIN, TARGET],
    ]
    assert _calls(run.log, "cargo") == [
        [
            "cargo",
            str(run.workspace),
            *words,
            f"CARGO_INSTALL_ROOT={work}/remap/agent-family",
            *shared,
            f"RUSTFLAGS={run.remap_flags}",
        ],
        [
            "cargo",
            str(run.workspace),
            *words,
            f"CARGO_INSTALL_ROOT={work}/plain/agent-family",
            *shared,
            "RUSTFLAGS=<unset>",
        ],
    ]


@needs_gnu_tar
def test_the_archive_has_the_same_bytes_for_each_pack_of_one_tree(tmp_path: Path) -> None:
    """Each member is a regular file `agent-family/bin/<program>` with a fixed
    owner, mode and time. gzip keeps no name and no time."""
    run = Build(tmp_path)
    name = f"agent-family-{TARGET}.tar.gz"
    archive = run.temp / "artifact-probe" / "pack-1" / name
    again = run.temp / "artifact-probe" / "pack-2" / name

    assert run.done.returncode == 0, run.said
    assert run.outputs["archive"] == str(archive)
    assert archive.read_bytes() == again.read_bytes()

    raw = archive.read_bytes()
    assert raw[:2] == b"\x1f\x8b"
    assert raw[3] == 0, "the gzip header holds a name or a comment"
    assert raw[4:8] == bytes(4), "the gzip header holds a time"

    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(raw))) as packed:
        members = packed.getmembers()
    (member,) = members
    assert member.name == "agent-family/bin/agent-family"
    assert member.isreg()
    assert (member.mode, member.uid, member.gid, member.mtime) == (0o755, 0, 0, 0)


@needs_gnu_tar
def test_the_digest_output_is_one_line_of_sha256sum_for_the_archive(tmp_path: Path) -> None:
    """The job `sign` gives the line to `sha256sum --check --strict` in the
    directory of the archive, so the line names the file with no directory."""
    run = Build(tmp_path)
    archive = Path(run.outputs["archive"])
    digest = run.outputs["digest"]

    assert run.done.returncode == 0, run.said
    assert digest == f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}"
    assert run.outputs["toolchain"] == TOOLCHAIN


@needs_gnu_tar
@pytest.mark.parametrize(
    ("overrides", "answer"),
    [
        ({}, "no"),
        ({"STUB_PLAIN_TEXT": "{home}/.cargo/registry/src/one.rs"}, "yes"),
        ({"STUB_PLAIN_FAILS": "1"}, "unknown"),
    ],
    ids=["no", "yes", "unknown"],
)
def test_the_build_with_no_remap_flag_reports_a_fact_and_stops_nothing(
    tmp_path: Path, overrides: dict[str, str], answer: str
) -> None:
    given = {key: value.format(home=tmp_path / "home") for key, value in overrides.items()}
    run = Build(tmp_path, **given)

    assert run.done.returncode == 0, run.said
    assert run.outputs["remap_needed"] == answer
    assert f"- remap_needed: {answer}\n" in run.summary.read_text(encoding="utf-8")
    assert "digest" in run.outputs


def test_a_tree_with_a_second_thing_beside_its_programs_stops_the_build(tmp_path: Path) -> None:
    """The archive holds programs only. A record file of cargo in the tree
    means that the build lost the flag `--no-track`."""
    run = Build(tmp_path, STUB_RECORD="1")

    assert run.done.returncode == 1, run.said
    assert "no regular file directly in bin/" in run.done.stderr
    assert "digest" not in run.outputs
