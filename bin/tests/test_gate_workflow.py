"""The PR gate's jobs together still run every test, and one check says so.

`.github/workflows/gate.yml` runs the suite as N shards on N runners. Three
things could shrink what a PR is tested against without one red line, and
each gets a check here instead of a memory:

1. **A shard that names paths.** Every shard runs the whole of
   `pyproject.toml`'s `testpaths` and keeps its share with `--shard K/N`
   (the root `conftest.py`). A run line that named suites would leave out
   the suite somebody adds to `testpaths` next.
2. **A K/N written by hand.** The run line reads the job's index and the
   job total from the matrix itself, so the shards are always 1..N of N.
3. **A `gate` check that is green over a job that did not run.** `gate`
   needs every other job, and reads each one's result against the scope.

A job is billed in whole minutes, so one short check rides in a job that
runs anyway, and stays pinned: on a docs PR the tests marked `docs` in
`lint`.

It also pins what the release executor and the sessions rely on: one
workflow named `gate`, every action pinned to a commit, every local action
in the tree, full history in every checkout.

`release.yml` runs the same jobs after the merge, and the same checks hold
it: the same shard command, a last job that needs every other one, and one
shared verdict (`.github/actions/verdict`), so a PR and its release cannot
judge the same results differently. Its tag step runs only after that
verdict, one allocation at a time, and only its last job may write.

The release has jobs that the gate has not. The `plan` job names each
binary component that gets a tag in the run, from the dry run of the tag
script. The `artifacts` job builds the archive of each such component
(`bin/build-artifacts.sh`), in each scope. The tests run the text of the
plan step against a stand-in dry run.
The `disks` job builds the files of the disks row
(`disks/bin/playpen-release-disks`), only when the plan names `disks`
(decision CH76 rule 5). The tests run the text of its build step against
a stand-in script.
The `sign` job signs each archive and each disk file. It holds the
identity token, and each word that it runs is in the workflow file. The
`release` job takes the three workflow artifacts and cosign, and the tag
step gets `ASSET_DIR` and `COSIGN`. The tests run the text of the sign step and of the cosign step
against a stand-in `curl`.

The `proc` job runs the process-level suite (`integration/proc`), which is
in no shard: `testpaths` does not hold it. The job builds the facade first,
and a test that skips is a failure there. A run in which every test skips
because the build is missing would be green and would judge nothing.

The `suites` job runs the old suite of `integration/`, in a pytest run of
its own. It has the checkout and the venv of the `proc` job, and no node.
Its last step reads
the JUnit report of each run, and it fails for a test that skipped. Both
workflows have the job. The last job of each file has a name of its own, and
each other job name is in both files.

The `agent` job runs the vitest suite and the type check of `agent/`. Both
workflows have the job, and the verdict leaves it out on a docs change.

The `facade` job runs the vitest suite and the type check of `facade/`. Its
`pnpm test` builds `dist/` first. Both workflows have the job, and the
verdict leaves it out on a docs change.

The `systemd-proof` job runs `bin/systemd-proof.sh` for a change that touches
a file of the proof: `systemd/` or the script.
On any other code change it skips its one step after the checkout and is
still a success. The job runs on the runner itself, with no container: only
there is systemd process 1. The script answers which change needs the proof,
and the scope runs the proof when the script gives no answer.

The `playpen-image` job runs `playpen/bin/playpen-ci`: it builds each
flavor and checks its env file. Both workflows run it before their final
job can pass.

The `kvm` job runs `bin/vm-smoke-setup.sh`, then each ignored test of
rust/ whose name holds `kvm_` (decisions CH8-4, CH36 G19). The test run
names no crate and no file, so a new kvm_ test needs no workflow edit. A
step before the run fails the job when no such test exists.

The `vmm-archive` job is the one job of the gate alone (decision CH76 rule
4). It builds the vmm archive with the `artifacts` job's script and holds
each program static. On a code change that touches no file of the vmm
build it skips each step after the checkout, as the `systemd-proof` job
does. The scope gives that answer.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from handover.artifacts import asset_names, disk_components, disk_files
from handover.catalog import ARCHIVE_SUFFIX, BUNDLE_SUFFIX, DISK_FLAVORS, DISK_SUFFIX, Repo
from handover.discovery import read_one

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
GATE = yaml.safe_load((WORKFLOWS / "gate.yml").read_text(encoding="utf-8"))
JOBS: dict[str, dict[str, Any]] = GATE["jobs"]
RELEASE = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
RELEASE_JOBS: dict[str, dict[str, Any]] = RELEASE["jobs"]

#: The one check sessions read in `gh pr checks`, and the workflow's name.
GATE_NAME = "gate"

#: The job that tags, and the workflow's name.
RELEASE_NAME = "release"

#: Each workflow's jobs with the name of its last one, the one that judges.
WORKFLOW_JOBS = [(JOBS, GATE_NAME), (RELEASE_JOBS, RELEASE_NAME)]
BY_NAME = [GATE_NAME, RELEASE_NAME]

#: Each key of each of the two files, by the name of its workflow. PyYAML
#: reads the key `on` as the boolean. One more key can change each job of one
#: file, for example `defaults` with a shell that runs no line of a step, or
#: `env`. The tests that hold a job equal in the two files read `jobs` only.
FILE_KEYS = {
    GATE_NAME: {"name", True, "permissions", "concurrency", "jobs"},
    RELEASE_NAME: {"name", True, "permissions", "env", "jobs"},
}

#: The one `env` key of the files: the pin of cosign in the release. To take
#: another cosign, compute the SHA-256 of the asset `cosign-linux-amd64` of
#: the new release and compare it with the file `cosign_checksums.txt` of
#: that release. Then change the two values here and in `release.yml`.
COSIGN_ENV = {
    "COSIGN_VERSION": "v3.1.3",
    "COSIGN_SHA256": "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
}

#: What starts the gate: a pull request, and a merge queue's group.
GATE_EVENTS = ["pull_request", "merge_group"]

#: One run per pull request head or per merge group, e.g.
#: `gate-rework/some-branch` or `gate-refs/heads/gh-readonly-queue/main/pr-12-<sha>`.
GATE_GROUP = "gate-${{ github.head_ref || github.ref }}"

SCOPE_ACTION = yaml.safe_load(
    (REPO / ".github" / "actions" / "scope" / "action.yml").read_text(encoding="utf-8")
)

#: The one copy of the verdict, and how a job hands it its needs.
VERDICT = "./.github/actions/verdict"
VERDICT_WITH = {"needs": "${{ toJSON(needs) }}"}
VERDICT_ACTION = yaml.safe_load(
    (REPO / ".github" / "actions" / "verdict" / "action.yml").read_text(encoding="utf-8")
)

#: The whole test command of a shard. No path, no `-k`, no `-m`: only its
#: share of everything `testpaths` holds.
SHARD_RUN = 'uv run pytest -n auto --dist worksteal --shard "$((INDEX + 1))/$TOTAL"'

#: Where that command's K and N come from: the matrix, never a literal.
SHARD_ENV = {"INDEX": "${{ strategy.job-index }}", "TOTAL": "${{ strategy.job-total }}"}

#: A job a docs PR leaves out, as `jobs.<job>.if` spells it.
ONLY_CODE = "needs.scope.outputs.scope == 'code'"

#: The job that proves the restart rule of the daemon units, and the whole
#: of the proof: one script.
SYSTEMD_JOB = "systemd-proof"
SYSTEMD_RUN = "bin/systemd-proof.sh"

#: The job that builds the image which the playpen release publishes.
PLAYPEN_JOB = "playpen-image"
PLAYPEN_RUN = "playpen/bin/playpen-ci"

#: The job that checks the Cargo workspace, and the three checks it runs in
#: rust/, in this order. `--locked`: a build that would change
#: rust/Cargo.lock fails.
RUST_JOB = "rust"
RUST_DIR = "rust"
RUST_RUNS = [
    "cargo fmt --check",
    "cargo clippy --all-targets --locked -- -D warnings",
    "cargo test --locked",
]

#: The KVM smoke job, the script that readies the runner, and the one test
#: run: each ignored test whose name holds `kvm_`, in every crate. The run
#: has `--no-fail-fast`, so one red test binary does not hide the next.
KVM_JOB = "kvm"
KVM_SETUP = "bin/vm-smoke-setup.sh"
KVM_RUN = "cargo test --locked --no-fail-fast -- --ignored kvm_"

#: The step before the test run. `cargo test` exits 0 when its filter
#: matches no test, so this list fails the job when no ignored kvm_ test
#: exists.
KVM_COUNT = (
    "count=$(cargo test --locked -q -- --ignored --list kvm_ | grep -c ': test$' || true)\n"
    'echo "ignored kvm_ tests: $count"\n'
    'test "$count" -ge 1\n'
)
KVM_ENV = {"CRECHE_KVM_DIR": "${{ runner.temp }}/kvm"}

#: The step of that job before the three checks. It refuses an edit or a
#: deletion of a frozen host API file against the commit the change starts
#: from (decision CH33 rule 8).
FROZEN_STEP = "frozen host api"
FROZEN_DIFF = (
    "git diff --no-renames --diff-filter=MDT --name-status --exit-code \\\n"
    "  \"$base\" HEAD -- 'rust/crates/wire/*host-api-*.json'"
)
FROZEN_ENV = {
    "GROUP_BASE": "${{ github.event.merge_group.base_sha }}",
    "BASE_REF": "${{ github.base_ref }}",
    "BEFORE": "${{ github.event.before }}",
}

#: The step of that job that a change with no file of the proof skips. Only
#: the answer `false` skips it. An answer that is missing, or that a rename
#: lost, runs the proof.
ONLY_SYSTEMD = "needs.scope.outputs.systemd != 'false'"

#: How the scope job hands that answer to the job.
SYSTEMD_OUTPUT = "${{ steps.scope.outputs.systemd }}"

#: The machine of the job: the hosted runner, with its own systemd.
SYSTEMD_RUNNER = "ubuntu-24.04"

#: Each key of the job. One more key can move the proof off the systemd of
#: the runner, for example `container`. One more key can also let a failed
#: proof pass, for example `continue-on-error`.
SYSTEMD_KEYS = {"needs", "if", "runs-on", "timeout-minutes", "steps"}

#: Each key of the step that runs the proof.
SYSTEMD_STEP_KEYS = {"run", "if"}

#: How each scope asks the script. The answer starts as `true`, and only a
#: call that succeeds makes it `false`. A script that is absent, or that
#: fails, thus runs the proof.
SYSTEMD_ASKED = {
    GATE_NAME: 'if bin/systemd-proof.sh --unchanged "$base" HEAD; then\n  systemd=false\nfi\n',
    RELEASE_NAME: (
        'if [[ "$passed" == 1 ]] && bin/systemd-proof.sh --unchanged "$BEFORE" "$GITHUB_SHA";'
        " then\n  systemd=false\nfi\n"
    ),
}

#: The catalog's name for this repository (`handover/src/handover/catalog.py`).
THIS_REPOSITORY = str(Repo.CRECHE)

#: The job of the release that builds the archive of each binary component.
ARTIFACTS_JOB = "artifacts"

#: The job of the release that signs each archive.
SIGN_JOB = "sign"

#: The job of the release that names each binary component with a planned
#: tag.
PLAN_JOB = "plan"

#: The job of the release that builds the files of the disks row, and that
#: row (decision CH76 rule 5).
DISKS_JOB = "disks"

#: The jobs that only the release has.
RELEASE_ONLY = {PLAN_JOB, ARTIFACTS_JOB, DISKS_JOB, SIGN_JOB}

#: The jobs of each file with no rule of their own. The verdict needs a
#: success from each of them in each scope.
NO_RULE = {GATE_NAME: {"scope", "lint"}, RELEASE_NAME: {"scope", "lint", *RELEASE_ONLY}}

#: The actions of the `artifacts` job, by their commit.
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_UV = "astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4"
UPLOAD_ACTION = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD_ACTION = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"

#: The days that a workflow artifact stays.
RETENTION_DAYS = 7

#: Where the archives, the bundles and cosign go on a runner.
ARTIFACT_DIR = "${{ runner.temp }}/artifacts"
ASSET_DIR = "${{ runner.temp }}/assets"
BUNDLE_DIR = "${{ runner.temp }}/bundles"
COSIGN_PATH = "${{ runner.temp }}/cosign"

#: Each key of the `artifacts` job. No `if`: the job runs in each scope. No
#: `permissions`: it holds the read-only token of its file.
ARTIFACTS_KEYS = {"needs", "runs-on", "timeout-minutes", "outputs", "steps"}

#: The one output of the job: the step output of the build.
ARTIFACTS_OUTPUTS = {"digests": "${{ steps.build.outputs.digests }}"}

#: The variables of the build step, each one.
BUILD_STEP_ENV = {
    "ARTIFACT_REPO": THIS_REPOSITORY,
    "ARTIFACT_PLANNED": "${{ needs.plan.outputs.components }}",
    "ARTIFACT_OUT": ARTIFACT_DIR,
}

#: The steps of the job, as a whole. The checkout keeps no token in the
#: clone, because a build script runs here. The step that takes uv keeps no
#: cache: the bytes of an archive come from a clean build.
ARTIFACTS_STEPS = [
    {"uses": CHECKOUT_ACTION, "with": {"fetch-depth": 0, "persist-credentials": False}},
    {"uses": SETUP_UV, "with": {"enable-cache": False}},
    {"name": "build", "id": "build", "env": BUILD_STEP_ENV, "run": "bin/build-artifacts.sh"},
    {
        "uses": UPLOAD_ACTION,
        "with": {
            "name": ARTIFACTS_JOB,
            "path": ARTIFACT_DIR,
            "if-no-files-found": "error",
            "retention-days": RETENTION_DAYS,
        },
    },
]

#: Where the files of the disks row go on a runner.
DISK_DIR = "${{ runner.temp }}/disks"

#: Each key of the `disks` job. No `if`: the job runs in each scope, and
#: with no planned disks row it builds nothing and passes. No
#: `permissions`: it holds the read-only token of its file.
DISKS_KEYS = {"needs", "runs-on", "timeout-minutes", "outputs", "steps"}

#: The one output of the job: the step output of the build.
DISKS_OUTPUTS = {"digests": "${{ steps.build.outputs.digests }}"}

#: The build argv of `disks/component.toml`, as one line of the step.
DISKS_ARGV = "/usr/bin/env bash disks/bin/playpen-release-disks"

#: The whole text of the build step of the `disks` job.
DISKS_RUN = f"""\
set -euo pipefail
if [[ " $PLANNED " == *" {DISKS_JOB} "* ]]; then
  {DISKS_ARGV}
else
  echo "disks: no planned disk-image component"
  mkdir -p "$DISK_OUT"
  : > "$DISK_OUT/SHA256SUMS"
fi
{{
  echo "digests<<END_OF_DIGESTS"
  cat "$DISK_OUT/SHA256SUMS"
  echo "END_OF_DIGESTS"
}} >> "$GITHUB_OUTPUT"
"""

#: The steps of the `disks` job, as a whole. The checkout keeps no token in
#: the clone, because a build script runs here. The upload does not
#: compress: each file is a zstd file already.
DISKS_STEPS = [
    {"uses": CHECKOUT_ACTION, "with": {"fetch-depth": 0, "persist-credentials": False}},
    {
        "name": "build",
        "id": "build",
        "env": {"PLANNED": "${{ needs.plan.outputs.components }}", "DISK_OUT": DISK_DIR},
        "run": DISKS_RUN,
    },
    {
        "uses": UPLOAD_ACTION,
        "with": {
            "name": DISKS_JOB,
            "path": DISK_DIR,
            "if-no-files-found": "error",
            "retention-days": RETENTION_DAYS,
            "compression-level": 0,
        },
    },
]

#: The job of the gate alone: the vmm archive, built unsigned on a PR that
#: changes a file of the vmm build (decision CH76 rule 4).
VMM_ARCHIVE_JOB = "vmm-archive"

#: The jobs that only the gate has.
GATE_ONLY = {VMM_ARCHIVE_JOB}

#: The steps of that job that a change with no file of the vmm build skips.
#: Only the answer `false` skips them.
ONLY_VMM = "needs.scope.outputs.vmm != 'false'"

#: How the scope job hands that answer to the job.
VMM_OUTPUT = "${{ steps.scope.outputs.vmm }}"

#: Each key of the job. One more key can let a failed build pass, for
#: example `continue-on-error`.
VMM_ARCHIVE_KEYS = {"needs", "if", "runs-on", "timeout-minutes", "steps"}

#: The catalog row the job builds, and its archive.
VMM_ROW = "vmm"
VMM_ARCHIVE = asset_names(VMM_ROW).archive

#: The text of the last step: the positive control, then each program of
#: the archive (bin/lib/elfstatic.sh).
VMM_STATIC_RUN = """\
set -euo pipefail
. bin/lib/elfstatic.sh
elf_static_refuses_dynamic
mkdir -- "$TREE"
tar --extract --gzip --file "$ARCHIVE" --directory "$TREE"
for program in vmm guest-init cloud-hypervisor ch-remote; do
  elf_static "$TREE/vmm/bin/$program"
done
echo "the four programs of the vmm archive are static"
"""

#: The steps of the job, as a whole. The build step is the build step of
#: the `artifacts` job, with the vmm row planned: no step more, so the job
#: shows what the release run would meet. Nothing uploads and nothing signs.
VMM_ARCHIVE_STEPS = [
    {"uses": CHECKOUT_ACTION, "with": {"fetch-depth": 0, "persist-credentials": False}},
    {"uses": SETUP_UV, "if": ONLY_VMM, "with": {"enable-cache": False}},
    {
        "name": "build",
        "if": ONLY_VMM,
        "env": {
            "ARTIFACT_REPO": THIS_REPOSITORY,
            "ARTIFACT_PLANNED": VMM_ROW,
            "ARTIFACT_OUT": ARTIFACT_DIR,
        },
        "run": "bin/build-artifacts.sh",
    },
    {
        "name": "static programs",
        "if": ONLY_VMM,
        "shell": "bash",
        "env": {"ARCHIVE": f"{ARTIFACT_DIR}/{VMM_ARCHIVE}", "TREE": "${{ runner.temp }}/vmm-tree"},
        "run": VMM_STATIC_RUN,
    },
]

#: Each key of the `plan` job. No `needs` and no `if`: it runs in each scope,
#: beside the scope job. No `permissions`: it holds the read-only token of
#: its file.
PLAN_KEYS = {"runs-on", "timeout-minutes", "outputs", "steps"}

#: The one output of the job: the components of the plan step.
PLAN_OUTPUTS = {"components": "${{ steps.plan.outputs.components }}"}

#: The whole text of the plan step. A dry run that fails leaves the plan
#: with the lines that it printed, and the tag step judges the rest.
PLAN_RUN = """\
set -euo pipefail
status=0
handover/bin/allocate-tags.sh --dry-run > "$RUNNER_TEMP/plan" || status=$?
cat "$RUNNER_TEMP/plan"
if [[ "$status" != 0 ]]; then
  echo "::warning::the dry run ended with status $status. The plan holds the lines that it printed."
fi
attach='^would attach [^ ]+ to ([a-z][a-z0-9-]*)-v[0-9]+[.][0-9]+[.][0-9]+$'
components=""
while IFS= read -r line; do
  if [[ "$line" =~ $attach && " $components " != *" ${BASH_REMATCH[1]} "* ]]; then
    components="${components:+$components }${BASH_REMATCH[1]}"
  fi
done < "$RUNNER_TEMP/plan"
# Only main signs: a manual run on another branch builds and signs
# nothing, so it leaves no public transparency record.
if [[ "${GITHUB_REF:-}" != refs/heads/main ]]; then
  echo "plan: ${GITHUB_REF:-no ref} is not main, so no archive is built or signed"
  components=""
fi
echo "components=$components" >> "$GITHUB_OUTPUT"
echo "plan: ${components:-no binary component}"
"""

#: The steps of the `plan` job, as a whole. The checkout takes every tag and
#: keeps no token. The dry run gets the catalog name and no token.
PLAN_STEPS = [
    {"uses": CHECKOUT_ACTION, "with": {"fetch-depth": 0, "persist-credentials": False}},
    {"uses": SETUP_UV, "with": {"enable-cache": False}},
    {"name": PLAN_JOB, "id": PLAN_JOB, "env": {"TAG_REPO": THIS_REPOSITORY}, "run": PLAN_RUN},
]

#: The names of the files of a disk-image row, in the order of
#: `artifacts.disk_files` (decision CH76 rules 5 and 7).
DISK_NAMES = tuple(one.name for one in disk_files())

#: The names that the `sign` job signs, as the alternatives of one regex
#: group: an archive of a binary row, then each file of a disk-image row.
SIGNED_REGEX = "|".join(
    [
        "[A-Za-z0-9][A-Za-z0-9._-]*" + ARCHIVE_SUFFIX.replace(".", "[.]"),
        *(name.replace(".", "[.]") for name in DISK_NAMES),
    ]
)

#: The same names as one extended glob of bash.
SIGNED_GLOB = "@(" + "|".join([f"*{ARCHIVE_SUFFIX}", *DISK_NAMES]) + ")"

#: The text that takes cosign. The `sign` job and the `release` job hold it.
#: With no asset it prints one line and fetches nothing, so a run with no
#: binary or disk-image component fetches no program of 141 MB. Else it
#: compares the SHA-256 of the program with the constant before the line
#: that makes the file a program, and that line is before the first run of
#: it.
COSIGN_FETCH = f"""\
cosign="$RUNNER_TEMP/cosign"
shopt -s extglob
if compgen -G "$RUNNER_TEMP/assets/{SIGNED_GLOB}" > /dev/null; then
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
else
  echo "cosign: no archive, so no fetch"
fi
"""

#: The whole text of the cosign step of the `release` job.
COSIGN_RUN = "set -euo pipefail\n" + COSIGN_FETCH

#: The three lines of the fetch that must keep their order.
COSIGN_COMPARE = 'if [[ "$found" != "$COSIGN_SHA256" ]]; then'
COSIGN_MODE = 'chmod 0755 "$cosign"'
COSIGN_FIRST_USE = '"$cosign" version'

#: The line of the `sign` step that compares each archive with the job
#: output of `artifacts`.
DIGEST_CHECK = '(cd "$assets" && sha256sum --check --strict <<< "$DIGESTS")'

#: The whole text of the `sign` step: the check of the archives, the fetch,
#: and one signature for each archive.
SIGN_RUN = (
    """\
set -euo pipefail
shopt -s dotglob
assets="$RUNNER_TEMP/assets"
bundles="$RUNNER_TEMP/bundles"
mkdir -p "$bundles"
"""
    + """\
if [[ -n "$DISK_DIGESTS" ]]; then
  if [[ -n "$DIGESTS" ]]; then
    DIGESTS+=$'\\n'
  fi
  DIGESTS+="$DISK_DIGESTS"
fi
"""
    + f"asset='({SIGNED_REGEX})'\n"
    + """\
line="^[0-9a-f]{64}  $asset\\$"
whole="^$asset\\$"
named=""
if [[ -n "$DIGESTS" ]]; then
  while IFS= read -r one; do
    if [[ ! "$one" =~ $line ]]; then
      echo "sign: the job artifacts gave a line that is no SHA-256 line of an archive" >&2
      exit 1
    fi
    named+="${one:66}"$'\\n'
  done <<< "$DIGESTS"
fi
files=""
for file in "$assets"/*; do
  name="${file##*/}"
  if [[ ! -e "$file" || "$name" == "SHA256SUMS" ]]; then
    continue
  fi
  if [[ -L "$file" || ! -f "$file" || ! "$name" =~ $whole ]]; then
    echo "sign: the job artifacts gave $name, which is no archive" >&2
    exit 1
  fi
  files+="$name"$'\\n'
done
if [[ "$(printf '%s' "$named" | sort -u)" != "$(printf '%s' "$named" | sort)" ]]; then
  echo "sign: the job artifacts named one archive two times" >&2
  exit 1
fi
if [[ "$(printf '%s' "$named" | sort)" != "$(printf '%s' "$files" | sort)" ]]; then
  echo "sign: the archives are not the archives that the job artifacts named" >&2
  exit 1
fi
if [[ -z "$named" ]]; then
  echo "sign: no archive"
  : > "$bundles/SHA256SUMS"
  exit 0
fi
"""
    + DIGEST_CHECK
    + "\n"
    + COSIGN_FETCH
    + """\
while IFS= read -r one; do
  name="${one:66}"
  "$cosign" sign-blob --yes --bundle "$bundles/$name.sigstore.json" "$assets/$name" < /dev/null
done <<< "$DIGESTS"
printf '%s\\n' "$DIGESTS" > "$bundles/SHA256SUMS"
"""
)

#: Each key of the `sign` job. No `if`: the job runs in each scope, and with
#: no archive it signs nothing and passes.
SIGN_KEYS = {"needs", "runs-on", "timeout-minutes", "permissions", "steps"}

#: The whole token of the `sign` job.
SIGN_PERMISSIONS = {"id-token": "write"}

#: The steps of the `sign` job, as a whole: the archives of the run, the one
#: run step and the bundles. No checkout and no local action.
SIGN_STEPS = [
    {"uses": DOWNLOAD_ACTION, "with": {"name": ARTIFACTS_JOB, "path": ASSET_DIR}},
    {"uses": DOWNLOAD_ACTION, "with": {"name": DISKS_JOB, "path": ASSET_DIR}},
    {
        "name": SIGN_JOB,
        "env": {
            "DIGESTS": "${{ needs.artifacts.outputs.digests }}",
            "DISK_DIGESTS": "${{ needs.disks.outputs.digests }}",
        },
        "run": SIGN_RUN,
    },
    {
        "uses": UPLOAD_ACTION,
        "with": {
            "name": "bundles",
            "path": BUNDLE_DIR,
            "if-no-files-found": "error",
            "retention-days": RETENTION_DAYS,
        },
    },
]

#: The time limit of the cosign step of the `release` job. That job holds
#: the group of one allocation at a time, so a fetch that hangs would hold
#: each later release run. The step comes before the tag step, so the limit
#: stops no allocation half way.
COSIGN_STEP_MINUTES = 10

#: The cosign step of the `release` job.
COSIGN_STEP = {"name": "cosign", "timeout-minutes": COSIGN_STEP_MINUTES, "run": COSIGN_RUN}

#: The steps of the `release` job between the verdict and the step that
#: takes uv: the three workflow artifacts into one directory, then cosign.
#: The `bundles` artifact comes last: its SHA256SUMS holds the lines of
#: both build jobs.
RELEASE_ASSET_STEPS = [
    {"uses": DOWNLOAD_ACTION, "with": {"name": ARTIFACTS_JOB, "path": ASSET_DIR}},
    {"uses": DOWNLOAD_ACTION, "with": {"name": DISKS_JOB, "path": ASSET_DIR}},
    {"uses": DOWNLOAD_ACTION, "with": {"name": "bundles", "path": ASSET_DIR}},
    COSIGN_STEP,
]

#: The variables of the tag step: the token, the catalog name, the assets of
#: a binary component and the program that checks their signature.
TAG_ENV = {
    "GH_TOKEN": "${{ github.token }}",
    "TAG_REPO": THIS_REPOSITORY,
    "ASSET_DIR": ASSET_DIR,
    "COSIGN": COSIGN_PATH,
}

#: The whole test command of the `proc` job, as `integration/proc/AGENTS.md`
#: gives it.
PROC_RUN = "uv run pytest integration/proc -m slow -n 4"

#: The switch that makes a skip a failure, and the file that reads it.
PROC_ENV = {"CRECHE_PROC_NO_SKIP": "1"}
PROC_SWITCHES = REPO / "integration" / "proc" / "proc_services.py"

#: The build of the `proc` job: the facade, which the exit test starts, and
#: where it runs. No vitest and no typecheck: the `facade` job runs those.
FACADE_BUILD = "pnpm install --frozen-lockfile && pnpm run build"
FACADE_BUILD_DIR = "facade"

#: The job that runs the old suite of `integration/`.
SUITES_JOB = "suites"

#: The job that runs the suite and the type check of `agent/`.
AGENT_JOB = "agent"

#: The whole run of that job, and where it runs. The module has no build.
AGENT_RUN = "pnpm install --frozen-lockfile && pnpm test && pnpm run typecheck"
AGENT_DIR = "agent"

#: The job that runs the suite and the type check of `facade/`.
FACADE_JOB = "facade"

#: The job of the memory package, which has its own venv (memory/AGENTS.md).
MEMORY_JOB = "memory"
MEMORY_DIR = "memory"
MEMORY_RUNS = ["uv sync --locked", "uv run pyright", "uv run pytest"]

#: The whole run of that job, and where it runs. `pnpm test` builds first.
FACADE_RUN = AGENT_RUN
FACADE_DIR = "facade"

#: The whole test command of each suite, as `integration/AGENTS.md` gives it,
#: by the name of its report. The job runs each command as it is, in a step of
#: its own.
SUITE_RUNS = {
    "tests_manager": "uv run pytest integration/tests_manager -m slow",
}

#: Where the runs of the job write their JUnit reports, and the option that
#: names one report. The option travels in a variable, so the command stays
#: as it is.
REPORT_DIR = "${{ runner.temp }}/suites"
REPORT_OPTION = "--junitxml="
REPORT_VARIABLE = "PYTEST_ADDOPTS"

#: The last step of the job, and the variable that gives it the reports. These
#: suites read no switch that makes a skip a failure, so this step reads the
#: report of each run.
NO_SKIP_STEP = "no test skipped"
NO_SKIP_KEYS = {"name", "shell", "env", "run"}
NO_SKIP_SHELL = "python3 {0}"
REPORTS_VARIABLE = "REPORTS"

#: Each key of the job. One more key can change what a failure of the job
#: does, for example `continue-on-error`.
SUITES_KEYS = {"needs", "if", "runs-on", "timeout-minutes", "steps"}

#: The time limit of the job in minutes: the limit of the `proc` job.
SUITES_MINUTES = 20

#: The time limit of the `proc` job. Each test of that suite has a limit of
#: its own, so a hang fails one test and the job ends.
PROC_MINUTES = 10

#: One small test file for each kind of pytest run that the last step judges.
SMALL_RUNS = {
    "ran": "def test_one():\n    pass\n",
    "one_skipped": (
        "import pytest\n\n\n"
        "def test_one():\n    pass\n\n\n"
        "def test_two():\n    pytest.skip('the bundle is absent')\n"
    ),
    "each_skipped": (
        "import pytest\n\n"
        "pytestmark = pytest.mark.skipif(True, reason='the bundle is absent')\n\n\n"
        "def test_one():\n    pass\n\n\n"
        "def test_two():\n    pass\n"
    ),
    "no_test": "ONE = 1\n",
}

#: A report that no run wrote, and a report that is no XML text.
ABSENT_REPORT = "absent"
BROKEN_REPORT = "broken"

#: The steps that give a job node and pnpm, by the start of `uses`.
NODE_ACTIONS = ("pnpm/action-setup@", "actions/setup-node@")

#: The local action that gives a job uv and the venv.
UV_SYNC = "./.github/actions/uv-sync"

#: The step of the `proc` job that keeps the pinned build of the Rust
#: chaperone between runs (integration/proc/proc_chaperone_pin.py). The
#: `suites` job builds no chaperone, so it has no such step.
CACHE_ACTION = "actions/cache@"
CHAPERONE_CACHE = {
    "path": "~/.cache/creche-proc/chaperone-rs",
    "key": (
        "proc-chaperone-rs-${{ runner.os }}-${{ hashFiles('integration/proc/chaperone.pin', "
        "'integration/proc/proc_chaperone_pin.py') }}"
    ),
}
CHAPERONE_PIN = REPO / "integration" / "proc" / "chaperone.pin"
CHAPERONE_BUILD = REPO / "integration" / "proc" / "proc_chaperone_pin.py"

#: What the lint job runs for each scope: lint alone beside the shards, and
#: lint with the tests marked `docs` where there is no shard. The gate's lint
#: job asks the scope itself, and the release's reads the scope job.
LINT_RUNS = {
    GATE_NAME: {
        "steps.scope.outputs.scope == 'code'": "bin/quality-gate.sh",
        "steps.scope.outputs.scope == 'docs'": "bin/quality-gate.sh --docs",
    },
    RELEASE_NAME: {
        "needs.scope.outputs.scope == 'code'": "bin/quality-gate.sh",
        "needs.scope.outputs.scope == 'docs'": "bin/quality-gate.sh --docs",
    },
}

#: The steps of the last job that decide, by name.
VERDICT_STEP = "every job passed"
TAG_STEP = "tag and release"

#: The whole of the tag step: the allocator, which holds the logic and its
#: own tests (handover/tests/test_handover_tag_script.py).
TAG_RUN = "handover/bin/allocate-tags.sh"

#: One allocation at a time, and none stopped half way.
ALLOCATE_ONE_AT_A_TIME = {"group": "release-allocate", "cancel-in-progress": False}

#: `uses: owner/action@<40 hex> # v1.2.3`, or a local `./path`.
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
TAG_COMMENT = re.compile(r"^\s*# v\d+\.\d+\.\d+$")

#: A small suite, to split for real in a subprocess.
SMALL_SUITE = "library/tests"


def _root_conftest() -> Any:
    """The root conftest.py as a module: `--shard` lives there."""
    spec = importlib.util.spec_from_file_location("root_conftest", REPO / "conftest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def _collected(*args: str) -> list[str]:
    """The test ids `pytest --collect-only` prints for `args`."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    return [line for line in done.stdout.splitlines() if "::" in line]


def _yaml_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *(REPO / ".github" / "actions").glob("*/action.yml")])


def _step(jobs: dict[str, dict[str, Any]], last: str, name: str) -> dict[str, Any]:
    (step,) = [one for one in jobs[last]["steps"] if one.get("name") == name]

    return step


def _gate_step(name: str) -> dict[str, Any]:
    return _step(JOBS, GATE_NAME, name)


def test_one_workflow_and_one_check_named_gate() -> None:
    assert GATE["name"] == GATE_NAME
    assert GATE_NAME in JOBS


def test_the_gate_runs_on_a_pull_request_and_on_a_merge_group() -> None:
    """A merge queue merges on the `gate` of the group it built. With no
    `merge_group` trigger that check never reports and the queue waits for
    ever. PyYAML reads the key `on` as the boolean."""
    assert GATE[True] == GATE_EVENTS


def test_two_merge_groups_never_share_a_concurrency_group() -> None:
    """`head_ref` is empty on a merge group. A group named by it alone is
    `gate-` for every entry of the queue, and each one cancels the last."""
    assert GATE["concurrency"] == {"group": GATE_GROUP, "cancel-in-progress": True}


def test_the_scope_of_a_merge_group_is_read_from_the_groups_base() -> None:
    (step,) = SCOPE_ACTION["runs"]["steps"]

    assert step["env"] == {"GROUP_BASE": "${{ github.event.merge_group.base_sha }}"}
    assert 'base="$GROUP_BASE"' in step["run"]
    assert 'docs_only "$base" HEAD' in step["run"]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_last_job_needs_every_other_job_and_always_runs(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    judge = jobs[last]

    assert sorted(judge["needs"]) == sorted(set(jobs) - {last})
    assert judge["if"] == "always()"


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_a_shard_names_no_path_and_no_number(jobs: dict[str, dict[str, Any]], last: str) -> None:
    tests = jobs["tests"]
    runs = [step for step in tests["steps"] if "run" in step]

    assert tests["if"] == ONLY_CODE
    assert [step["run"] for step in runs] == [SHARD_RUN]
    assert runs[0]["env"] == SHARD_ENV


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_docs_scope_runs_no_shard_and_no_node_job(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    by_scope = {name: job.get("if") for name, job in jobs.items() if name != last}
    only_code = {name for name, rule in by_scope.items() if rule == ONLY_CODE}
    alone = GATE_ONLY if last == GATE_NAME else set()

    assert only_code == {
        "tests",
        AGENT_JOB,
        FACADE_JOB,
        "proc",
        SUITES_JOB,
        MEMORY_JOB,
        PLAYPEN_JOB,
        RUST_JOB,
        SYSTEMD_JOB,
        KVM_JOB,
        *alone,
    }
    assert only_code == set(DOCS) & set(jobs), "the verdict table of this file names other jobs"
    assert {name for name, rule in by_scope.items() if rule is None} == NO_RULE[last]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_lint_runs_the_docs_tests_on_a_docs_change_and_no_test_beside_the_shards(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    runs = {step["if"]: step["run"] for step in jobs["lint"]["steps"] if "run" in step}

    assert runs == LINT_RUNS[last]


def test_the_release_runs_the_gates_test_jobs() -> None:
    """A change to one file's shards, node steps, process suite, old suites
    or systemd proof that misses the other would let a merge pass a release
    its PR could not, or the reverse."""
    for name in (
        "tests",
        AGENT_JOB,
        FACADE_JOB,
        "proc",
        SUITES_JOB,
        MEMORY_JOB,
        PLAYPEN_JOB,
        RUST_JOB,
        SYSTEMD_JOB,
        KVM_JOB,
    ):
        assert RELEASE_JOBS[name] == JOBS[name], f"release.yml's {name} job is not gate.yml's"


def test_the_two_workflows_have_the_same_jobs() -> None:
    """The release runs each job of the gate but GATE_ONLY before its last
    job, and the jobs of RELEASE_ONLY. Another job that enters one file
    alone fails here. So the last job of the release needs as many jobs
    more than the last job of the gate as RELEASE_ONLY holds, and as many
    fewer as GATE_ONLY holds."""
    assert set(JOBS) - {GATE_NAME, *GATE_ONLY} == set(RELEASE_JOBS) - {RELEASE_NAME, *RELEASE_ONLY}
    assert not RELEASE_ONLY & set(JOBS)
    assert not GATE_ONLY & set(RELEASE_JOBS)
    assert len(RELEASE_JOBS[RELEASE_NAME]["needs"]) == len(JOBS[GATE_NAME]["needs"]) - len(
        GATE_ONLY
    ) + len(RELEASE_ONLY)


def test_the_vmm_archive_job_is_pinned_as_a_whole() -> None:
    """Each key of the job and each step, in their order (decision CH76
    rule 4). The build step runs the script of the `artifacts` job, with
    its uv, for the vmm row of the catalog."""
    job = JOBS[VMM_ARCHIVE_JOB]
    artifacts_build = [one for one in RELEASE_JOBS[ARTIFACTS_JOB]["steps"] if "run" in one]

    assert set(job) == VMM_ARCHIVE_KEYS
    assert job["needs"] == "scope"
    assert job["if"] == ONLY_CODE
    assert job["runs-on"] == "ubuntu-24.04"
    assert 0 < job["timeout-minutes"] <= RELEASE_JOBS[ARTIFACTS_JOB]["timeout-minutes"]
    assert job["steps"] == VMM_ARCHIVE_STEPS
    assert [one["run"] for one in artifacts_build] == [VMM_ARCHIVE_STEPS[2]["run"]]
    assert (REPO / "bin" / "lib" / "elfstatic.sh").is_file()


def test_the_vmm_archive_job_checks_each_program_of_the_vmm_row() -> None:
    """The program list of the last step is the list of `--bin` words of
    the vmm manifest (CH7-5)."""
    manifest = read_one(REPO, VMM_ROW).manifest
    programs = [
        argv[at + 1] for argv in manifest.build for at, word in enumerate(argv) if word == "--bin"
    ]

    assert f"for program in {' '.join(programs)}; do\n" in VMM_STATIC_RUN
    assert len(programs) == 4


def test_the_scope_action_gives_the_vmm_answer_as_an_output() -> None:
    assert SCOPE_ACTION["outputs"]["vmm"]["value"] == VMM_OUTPUT
    assert JOBS["scope"]["outputs"]["vmm"] == VMM_OUTPUT


def test_the_artifacts_job_is_pinned_as_a_whole() -> None:
    """Each key of the job and each step, in their order. The job has no
    rule and no `permissions` key, and no step has a rule. One more step
    can put another file into the upload, and one more key can let a failed
    step pass. The checkout keeps no token, and the job keeps no cache."""
    job = RELEASE_JOBS[ARTIFACTS_JOB]

    assert set(job) == ARTIFACTS_KEYS
    assert job["needs"] == ["scope", PLAN_JOB]
    assert job["runs-on"] == "ubuntu-24.04"
    assert 0 < job["timeout-minutes"] <= 30
    assert job["outputs"] == ARTIFACTS_OUTPUTS
    assert job["steps"] == ARTIFACTS_STEPS
    assert (REPO / "bin" / "build-artifacts.sh").is_file()


def test_the_disks_job_is_pinned_as_a_whole() -> None:
    """Each key of the job and each step, in their order (decision CH76
    rule 5). The job has no rule and no `permissions` key, so the verdict
    needs its success in each scope."""
    job = RELEASE_JOBS[DISKS_JOB]

    assert set(job) == DISKS_KEYS
    assert job["needs"] == ["scope", PLAN_JOB]
    assert job["runs-on"] == "ubuntu-24.04"
    assert 0 < job["timeout-minutes"] <= 30
    assert job["outputs"] == DISKS_OUTPUTS
    assert job["steps"] == DISKS_STEPS
    assert DISKS_JOB in RELEASE_JOBS[RELEASE_NAME]["needs"]


def test_the_disks_job_runs_the_build_of_the_disks_manifest() -> None:
    """One source for the build: the argv of `disks/component.toml`. The
    job builds the one disk-image row of this repository."""
    manifest = read_one(REPO, DISKS_JOB).manifest

    assert disk_components(Repo.CRECHE) == (DISKS_JOB,)
    assert manifest.build == (tuple(DISKS_ARGV.split()),)
    assert f"\n  {DISKS_ARGV}\n" in DISKS_RUN
    assert (REPO / manifest.build[0][-1]).is_file()


#: A stand-in build script: it writes STUB_SUMS as SHA256SUMS into DISK_OUT,
#: and one line with each word it got to STUB_LOG.
DISK_BUILD_STUB = """#!/usr/bin/env bash
mkdir -p "$DISK_OUT"
printf '%s\\n' "$STUB_SUMS" > "$DISK_OUT/SHA256SUMS"
echo "build $*" >> "$STUB_LOG"
"""

#: (the plan, whether the build runs, the output `digests`).
DISK_PLANS = {
    "no-plan": ("", False, ""),
    "a-vmm-tag": ("vmm", False, ""),
    "a-disks-tag": ("vmm disks", True, "a line"),
    "a-name-that-holds-disks": ("disks-old", False, ""),
}


@pytest.mark.parametrize(("planned", "built", "digests"), DISK_PLANS.values(), ids=DISK_PLANS)
def test_the_disks_step_builds_only_a_planned_disks_row(
    tmp_path: Path, planned: str, built: bool, digests: str
) -> None:
    """The step runs the build only when the plan names `disks` as one
    word. Else it writes an empty SHA256SUMS. The output `digests` holds
    the text of SHA256SUMS."""
    script = tmp_path / "disks" / "bin" / "playpen-release-disks"
    script.parent.mkdir(parents=True)
    script.write_text(DISK_BUILD_STUB, encoding="utf-8")
    output = tmp_path / "output"
    log = tmp_path / "log"
    out = tmp_path / "temp" / "disks"
    done = subprocess.run(
        ["bash", "-c", DISKS_RUN],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PLANNED": planned,
            "DISK_OUT": str(out),
            "GITHUB_OUTPUT": str(output),
            "STUB_SUMS": "a line",
            "STUB_LOG": str(log),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert log.exists() == built
    assert (out / "SHA256SUMS").read_text(encoding="utf-8") == (f"{digests}\n" if built else "")
    body = f"{digests}\n" if digests else ""
    assert output.read_text(encoding="utf-8") == f"digests<<END_OF_DIGESTS\n{body}END_OF_DIGESTS\n"


def test_the_plan_job_is_pinned_as_a_whole() -> None:
    """The job holds the whole text of its run step. It needs no job, so
    no failed job before it can leave the plan out. The tag step and the
    plan name one catalog repository."""
    job = RELEASE_JOBS[PLAN_JOB]

    assert set(job) == PLAN_KEYS
    assert job["runs-on"] == "ubuntu-24.04"
    assert 0 < job["timeout-minutes"] <= 10
    assert job["outputs"] == PLAN_OUTPUTS
    assert job["steps"] == PLAN_STEPS
    assert _step(RELEASE_JOBS, RELEASE_NAME, TAG_STEP)["env"]["TAG_REPO"] == THIS_REPOSITORY


#: A stand-in tag script: it prints the file STUB_PLAN and ends with
#: STUB_STATUS.
DRY_RUN_STUB = """#!/usr/bin/env bash
[[ "$*" == "--dry-run" ]] || exit 9
cat "$STUB_PLAN"
exit "${STUB_STATUS:-0}"
"""

#: The lines of a dry run that plans one venv tag and one tag of `vmm`.
VMM_PLAN = """\
repo:    creche
create caregiver-v0.1.67 0.1.66 patch
create vmm-v0.1.0 - first
would create caregiver-v0.1.67 on abc and publish its Release
would create vmm-v0.1.0 on abc and publish its Release
would attach vmm-x86_64-unknown-linux-musl.tar.gz to vmm-v0.1.0
would attach vmm-x86_64-unknown-linux-musl.tar.gz.sigstore.json to vmm-v0.1.0
"""

#: (the lines of the dry run, its status, the output `components`).
PLANS = {
    "no-tag": ("allocate-tags: 0 created\n", "0", ""),
    "a-venv-tag-only": (VMM_PLAN.split("would create vmm")[0], "0", ""),
    "a-vmm-tag": (VMM_PLAN, "0", "vmm"),
    "two-binary-tags": (
        VMM_PLAN + "would attach other-x.tar.gz to other-v1.2.3\n",
        "0",
        "vmm other",
    ),
    "a-dry-run-that-fails-after-its-plan": (VMM_PLAN, "1", "vmm"),
    "a-dry-run-that-fails-first": ("", "1", ""),
    "lines-in-another-form": (
        "would attach a b to vmm-v1.2.3\nwould attach a to vmm-v1.2\n"
        "would attach a to Vmm-v1.2.3\n would attach a to vmm-v1.2.3\n",
        "0",
        "",
    ),
}


def _run_plan(
    tmp_path: Path, lines: str, status: str, ref: str = "refs/heads/main"
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the plan step against DRY_RUN_STUB that prints `lines` and ends
    with `status`. Return the run and the text of GITHUB_OUTPUT."""
    script = tmp_path / "handover" / "bin" / "allocate-tags.sh"
    script.parent.mkdir(parents=True)
    script.write_text(DRY_RUN_STUB, encoding="utf-8")
    script.chmod(0o755)
    plan = tmp_path / "given-plan"
    plan.write_text(lines, encoding="utf-8")
    output = tmp_path / "output"
    temp = tmp_path / "temp"
    temp.mkdir()
    done = subprocess.run(
        ["bash", "-c", PLAN_RUN],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(temp),
            "GITHUB_OUTPUT": str(output),
            "STUB_PLAN": str(plan),
            "STUB_STATUS": status,
            "GITHUB_REF": ref,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    return done, output.read_text(encoding="utf-8")


@pytest.mark.parametrize(("lines", "status", "components"), PLANS.values(), ids=PLANS)
def test_the_plan_step_names_each_binary_component_with_a_planned_tag(
    tmp_path: Path, lines: str, status: str, components: str
) -> None:
    """The step reads the `would attach` lines of the dry run. A line in
    another form names no component. The step passes also when the dry run
    fails: the tag step judges the same tree."""
    done, output = _run_plan(tmp_path, lines, status)

    assert done.returncode == 0, done.stdout + done.stderr
    assert output == f"components={components}\n"
    assert done.stdout.startswith(lines)
    assert ("::warning::" in done.stdout) == (status != "0")


#: The one line of the tag script that names an asset in a dry run.
ATTACH_LINE = 'say "would attach $name to $tag"'


def test_the_plan_step_reads_the_attach_line_of_the_real_tag_script(tmp_path: Path) -> None:
    """The plan step and the tag script agree on one line. The script holds
    ATTACH_LINE and no other `would attach` line. That line, with the real
    asset names of vmm, gives the attach lines of VMM_PLAN, and the plan step
    names vmm from them. A change to the line in the script fails here, so
    the plan never drops a binary component without a red test."""
    script = (REPO / "handover" / "bin" / "allocate-tags.sh").read_text(encoding="utf-8")
    said = [line.strip() for line in script.splitlines() if "would attach" in line]
    assert said == [ATTACH_LINE]

    template = ATTACH_LINE.removeprefix('say "').removesuffix('"')
    names = asset_names("vmm")
    lines = [
        template.replace("$name", name).replace("$tag", "vmm-v0.1.0")
        for name in (names.archive, names.bundle)
    ]
    assert lines == [line for line in VMM_PLAN.splitlines() if line.startswith("would attach")]

    done, output = _run_plan(tmp_path, "".join(f"{line}\n" for line in lines), "0")

    assert done.returncode == 0, done.stdout + done.stderr
    assert output == "components=vmm\n"


def test_the_artifacts_job_takes_uv_at_the_pin_of_the_release_job() -> None:
    (pinned,) = [
        step for step in RELEASE_JOBS[RELEASE_NAME]["steps"] if step.get("uses") == SETUP_UV
    ]

    assert pinned == {"uses": SETUP_UV}


def test_the_sign_job_is_pinned_as_a_whole() -> None:
    """The job holds the identity token. Each word that it runs is in the
    workflow file: the test holds each step, each key and the whole text of
    the one run step. No step starts a file of the repository, and no text
    holds an expression that a value of the run fills in."""
    job = RELEASE_JOBS[SIGN_JOB]
    (step,) = [one for one in job["steps"] if "run" in one]

    assert set(job) == SIGN_KEYS
    assert job["needs"] == [ARTIFACTS_JOB, DISKS_JOB]
    assert 0 < job["timeout-minutes"] <= 15
    assert job["steps"] == SIGN_STEPS
    assert "bin/" not in step["run"]
    assert "${{" not in step["run"]


def test_the_sign_job_names_the_assets_as_the_catalog_does() -> None:
    """The tag script takes the names of the assets from the catalog
    (`handover allocate-tags --assets`). The `sign` job writes the bundle
    and finds each archive by the two suffixes of the catalog, and each
    file of a disk-image row by its name in `artifacts.disk_files`. So a
    change of one constant fails here before a release run refuses each
    component that ships assets."""
    archive_end = ARCHIVE_SUFFIX.replace(".", "[.]")

    assert f'"$bundles/$name{BUNDLE_SUFFIX}"' in SIGN_RUN
    assert f"asset='([A-Za-z0-9][A-Za-z0-9._-]*{archive_end}|" in SIGN_RUN
    assert 'line="^[0-9a-f]{64}  $asset\\$"\n' in SIGN_RUN
    assert '"$name" =~ $whole ]]' in SIGN_RUN
    assert f'compgen -G "$RUNNER_TEMP/assets/@(*{ARCHIVE_SUFFIX}|' in COSIGN_FETCH
    for name in DISK_NAMES:
        assert f"|{name.replace('.', '[.]')}" in SIGN_RUN
        assert f"|{name}" in COSIGN_FETCH
    assert COSIGN_FETCH in SIGN_RUN
    assert COSIGN_FETCH in COSIGN_RUN


def test_the_disk_names_are_the_disk_flavors() -> None:
    """The workflow file names one root disk for each flavor of
    `catalog.DISK_FLAVORS`, in the regex of the sign step and in the glob
    of each cosign fetch, and no other (decision CH76 rule 7). The test
    reads the text of the workflow, not the pins of this file."""
    sign = _step(RELEASE_JOBS, SIGN_JOB, SIGN_JOB)["run"]
    fetch = _step(RELEASE_JOBS, RELEASE_NAME, "cosign")["run"]
    disk_end = DISK_SUFFIX.replace(".", "[.]")
    in_regex = re.findall(rf"\|([a-z]+){re.escape(disk_end)}", sign)
    in_globs = [re.findall(rf"\|([a-z]+){re.escape(DISK_SUFFIX)}", text) for text in (sign, fetch)]

    assert sorted(in_regex) == sorted(DISK_FLAVORS)
    assert [sorted(one) for one in in_globs] == [sorted(DISK_FLAVORS)] * 2


def test_the_digest_check_is_before_the_first_cosign_word() -> None:
    """The step compares each archive with the job output before it takes
    cosign. In the fetch the compare of the program comes before the line
    that makes the file a program, and that line before the first run."""
    first = re.search(r"\bcosign\b", SIGN_RUN)

    assert first is not None
    assert SIGN_RUN.index(DIGEST_CHECK) < first.start()
    assert (
        COSIGN_FETCH.index(COSIGN_COMPARE)
        < COSIGN_FETCH.index(COSIGN_MODE)
        < COSIGN_FETCH.index(COSIGN_FIRST_USE)
    )


def test_the_sign_job_and_the_release_job_take_one_cosign() -> None:
    """One text takes cosign in the two jobs, with the two constants of the
    file. The release job then checks a signature with the program that the
    sign job signed with."""
    (step,) = [one for one in RELEASE_JOBS[RELEASE_NAME]["steps"] if one.get("name") == "cosign"]

    assert RELEASE["env"] == COSIGN_ENV
    assert step == COSIGN_STEP
    assert COSIGN_FETCH in SIGN_RUN
    for name, job in RELEASE_JOBS.items():
        for one in job["steps"]:
            assert not set(one.get("env", {})) & set(COSIGN_ENV), f"{name} sets a constant again"


def test_the_release_job_takes_the_assets_after_the_verdict_and_before_the_tag() -> None:
    """The two workflow artifacts go into one directory, and cosign comes
    after them: the fetch reads that directory. The tag step gets both."""
    steps = RELEASE_JOBS[RELEASE_NAME]["steps"]
    names = [step.get("name") or step.get("uses") for step in steps]
    verdict = names.index(VERDICT_STEP)
    tag = names.index(TAG_STEP)

    assert steps[verdict + 1 : verdict + 1 + len(RELEASE_ASSET_STEPS)] == RELEASE_ASSET_STEPS
    assert verdict + len(RELEASE_ASSET_STEPS) < tag
    assert _step(RELEASE_JOBS, RELEASE_NAME, TAG_STEP)["env"] == TAG_ENV
    assert _step(RELEASE_JOBS, RELEASE_NAME, TAG_STEP)["run"] == TAG_RUN


def test_each_upload_fails_with_no_file_and_stays_seven_days() -> None:
    uploads = [
        step
        for job in RELEASE_JOBS.values()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]

    assert uploads
    for step in uploads:
        assert step["uses"] == UPLOAD_ACTION
        assert step["with"]["if-no-files-found"] == "error"
        assert step["with"]["retention-days"] == RETENTION_DAYS
    for job in RELEASE_JOBS.values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/download-artifact@"):
                assert step["uses"] == DOWNLOAD_ACTION


#: What a run of the `sign` step needs of a runner: `sha256sum --check
#: --strict`. A case that needs it skips on a development machine without
#: it, and fails in CI.
IN_CI = bool(os.environ.get("CI"))


def _checks_a_line() -> bool:
    """Whether the `sha256sum` of this machine takes `--check --strict` and a
    line on its standard input, as the one of a runner does."""
    program = shutil.which("sha256sum")
    if program is None:
        return False

    done = subprocess.run(
        [program, "--check", "--strict"],
        input=f"{hashlib.sha256(b'').hexdigest()}  /dev/null\n",
        capture_output=True,
        text=True,
        check=False,
    )

    return done.returncode == 0


needs_sha256sum_check = pytest.mark.skipif(
    not IN_CI and not _checks_a_line(), reason="the step uses `sha256sum --check --strict`"
)

#: `curl`: it writes its words to STUB_LOG and puts STUB_PROGRAM at the path
#: after `--output`.
CURL_STUB = """#!/usr/bin/env bash
printf '%s\\037' curl "$@" >> "$STUB_LOG"
printf '\\n' >> "$STUB_LOG"
while [[ "$#" -gt 0 ]]; do
  if [[ "$1" == "--output" ]]; then
    cp "$STUB_PROGRAM" "$2"
  fi
  shift
done
"""

#: What the stub `curl` fetches: a program that writes its words to
#: STUB_USED, and writes a bundle at the path after `--bundle`.
FETCHED = """#!/bin/sh
echo "$*" >> "$STUB_USED"
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--bundle" ]; then
    echo "a bundle" > "$2"
  fi
  shift
done
"""

#: Two archives and their lines of `sha256sum`.
ONE = "one-x86_64-unknown-linux-musl.tar.gz"
TWO = "two-x86_64-unknown-linux-musl.tar.gz"
BYTES = {ONE: b"the first archive", TWO: b"the second archive"}
LINES = {name: f"{hashlib.sha256(body).hexdigest()}  {name}" for name, body in BYTES.items()}

#: The files of a disk-image row, and their lines of `sha256sum`.
DISK_BYTES = {name: f"the file {name}".encode() for name in DISK_NAMES}
DISK_LINES = "\n".join(
    f"{hashlib.sha256(body).hexdigest()}  {name}" for name, body in DISK_BYTES.items()
)


class Fetch:
    """One run of the text of a step that takes cosign, on a runner whose
    temporary directory holds `files` in `assets/`."""

    def __init__(
        self,
        tmp_path: Path,
        text: str,
        files: dict[str, bytes],
        digests: str = "",
        sha256: str | None = None,
        disk_digests: str = "",
    ) -> None:
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        curl = stubs / "curl"
        curl.write_text(CURL_STUB, encoding="utf-8")
        curl.chmod(0o755)
        fetched = tmp_path / "fetched"
        fetched.write_text(FETCHED, encoding="utf-8")

        self.temp = tmp_path / "temp"
        self.assets = self.temp / "assets"
        self.assets.mkdir(parents=True)
        for name, body in files.items():
            (self.assets / name).write_bytes(body)

        self.bundles = self.temp / "bundles"
        self.program = self.temp / "cosign"
        self.log = tmp_path / "calls.log"
        self.used = tmp_path / "used"
        self.done = subprocess.run(
            ["bash", "-c", text],
            env={
                "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
                "RUNNER_TEMP": str(self.temp),
                "DIGESTS": digests,
                "DISK_DIGESTS": disk_digests,
                "COSIGN_VERSION": COSIGN_ENV["COSIGN_VERSION"],
                "COSIGN_SHA256": sha256 or hashlib.sha256(FETCHED.encode()).hexdigest(),
                "STUB_LOG": str(self.log),
                "STUB_PROGRAM": str(fetched),
                "STUB_USED": str(self.used),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    @property
    def fetches(self) -> int:
        if not self.log.exists():
            return 0

        return len(self.log.read_text(encoding="utf-8").splitlines())


def test_with_no_archive_the_sign_step_signs_nothing_and_fetches_nothing(tmp_path: Path) -> None:
    """The catalog of today holds no binary component. The step prints one
    line and uploads an empty SHA256SUMS: the release job cannot download an
    artifact that does not exist."""
    run = Fetch(tmp_path, SIGN_RUN, {"SHA256SUMS": b""})

    assert run.done.returncode == 0, run.done.stdout + run.done.stderr
    assert run.done.stdout == "sign: no archive\n"
    assert sorted(path.name for path in run.bundles.iterdir()) == ["SHA256SUMS"]
    assert (run.bundles / "SHA256SUMS").read_bytes() == b""
    assert run.fetches == 0


@needs_sha256sum_check
def test_the_sign_step_signs_each_archive_that_the_job_output_names(tmp_path: Path) -> None:
    digests = f"{LINES[ONE]}\n{LINES[TWO]}"
    run = Fetch(tmp_path, SIGN_RUN, {**BYTES, "SHA256SUMS": f"{digests}\n".encode()}, digests)

    assert run.done.returncode == 0, run.done.stdout + run.done.stderr
    assert run.fetches == 1
    assert run.used.read_text(encoding="utf-8").splitlines() == [
        "version",
        f"sign-blob --yes --bundle {run.bundles}/{ONE}.sigstore.json {run.assets}/{ONE}",
        f"sign-blob --yes --bundle {run.bundles}/{TWO}.sigstore.json {run.assets}/{TWO}",
    ]
    assert sorted(path.name for path in run.bundles.iterdir()) == sorted(
        ["SHA256SUMS", f"{ONE}.sigstore.json", f"{TWO}.sigstore.json"]
    )
    assert (run.bundles / "SHA256SUMS").read_text(encoding="utf-8") == f"{digests}\n"


#: (the files of the artifact, the job output, the line of the refusal).
#: Each one stops the step before it takes cosign.
REFUSED_SIGNS: dict[str, tuple[dict[str, bytes], str, str]] = {
    "an-absent-archive": ({}, LINES[ONE], "the archives are not the archives"),
    "an-archive-that-no-line-names": (BYTES, LINES[ONE], "the archives are not the archives"),
    "an-archive-with-no-line": ({ONE: BYTES[ONE]}, "", "the archives are not the archives"),
    "a-file-that-is-no-archive": (
        {ONE: BYTES[ONE], "notes.txt": b""},
        LINES[ONE],
        "gave notes.txt, which is no archive",
    ),
    "a-hidden-archive": (
        {ONE: BYTES[ONE], f".{TWO}": b""},
        LINES[ONE],
        f"gave .{TWO}, which is no archive",
    ),
    "no-sha256-line": ({ONE: BYTES[ONE]}, ONE, "no SHA-256 line of an archive"),
    "an-option-as-a-name": (
        {"-rf.tar.gz": b""},
        LINES[ONE].replace(ONE, "-rf.tar.gz"),
        "no SHA-256 line",
    ),
    "a-path-as-a-name": (
        {ONE: BYTES[ONE]},
        LINES[ONE].replace(ONE, f"../{ONE}"),
        "no SHA-256 line",
    ),
    "one-name-two-times": ({ONE: BYTES[ONE]}, f"{LINES[ONE]}\n{LINES[ONE]}", "two times"),
    "a-disk-of-no-flavor": (
        {f"coding{DISK_SUFFIX}": b""},
        LINES[ONE].replace(ONE, f"coding{DISK_SUFFIX}"),
        "no SHA-256 line",
    ),
    "a-file-of-no-flavor": (
        {ONE: BYTES[ONE], f"coding{DISK_SUFFIX}": b""},
        LINES[ONE],
        f"gave coding{DISK_SUFFIX}, which is no archive",
    ),
}


@needs_sha256sum_check
def test_the_sign_job_takes_the_disk_file_names(tmp_path: Path) -> None:
    """A run that ships the files of a disk-image row alone: the output of
    the `disks` job names them, and the step fetches cosign and signs each
    file into its own bundle (decision CH76 rule 5)."""
    run = Fetch(
        tmp_path,
        SIGN_RUN,
        {**DISK_BYTES, "SHA256SUMS": f"{DISK_LINES}\n".encode()},
        disk_digests=DISK_LINES,
    )

    assert run.done.returncode == 0, run.done.stdout + run.done.stderr
    assert run.fetches == 1
    assert run.used.read_text(encoding="utf-8").splitlines() == [
        "version",
        *(
            f"sign-blob --yes --bundle {run.bundles}/{name}{BUNDLE_SUFFIX} {run.assets}/{name}"
            for name in DISK_NAMES
        ),
    ]
    assert sorted(path.name for path in run.bundles.iterdir()) == sorted(
        ["SHA256SUMS", *(f"{name}{BUNDLE_SUFFIX}" for name in DISK_NAMES)]
    )
    assert (run.bundles / "SHA256SUMS").read_text(encoding="utf-8") == f"{DISK_LINES}\n"


@needs_sha256sum_check
def test_the_sign_step_signs_the_files_that_both_job_outputs_name(tmp_path: Path) -> None:
    """The `artifacts` job names an archive and the `disks` job names the
    disk files. The step signs each one, the archive first, and the
    SHA256SUMS of the bundles holds the lines of both, which replaces the
    two others in the `release` job."""
    run = Fetch(
        tmp_path,
        SIGN_RUN,
        {ONE: BYTES[ONE], **DISK_BYTES, "SHA256SUMS": b""},
        LINES[ONE],
        disk_digests=DISK_LINES,
    )

    assert run.done.returncode == 0, run.done.stdout + run.done.stderr
    assert run.fetches == 1
    assert run.used.read_text(encoding="utf-8").splitlines() == [
        "version",
        *(
            f"sign-blob --yes --bundle {run.bundles}/{name}{BUNDLE_SUFFIX} {run.assets}/{name}"
            for name in (ONE, *DISK_NAMES)
        ),
    ]
    assert (run.bundles / "SHA256SUMS").read_text(encoding="utf-8") == (
        f"{LINES[ONE]}\n{DISK_LINES}\n"
    )


def test_a_disk_file_that_no_output_names_is_refused(tmp_path: Path) -> None:
    """The `disks` artifact holds a file, and the `disks` job output names
    none: the step stops before it fetches cosign."""
    run = Fetch(tmp_path, SIGN_RUN, {**DISK_BYTES, "SHA256SUMS": b""})

    assert run.done.returncode == 1
    assert "sign: the archives are not the archives that the job artifacts named" in (
        run.done.stderr
    )
    assert run.fetches == 0


@pytest.mark.parametrize(("files", "digests", "fault"), REFUSED_SIGNS.values(), ids=REFUSED_SIGNS)
def test_the_sign_step_refuses_a_set_that_the_job_output_does_not_name(
    tmp_path: Path, files: dict[str, bytes], digests: str, fault: str
) -> None:
    """Each job of a run can write a workflow artifact. So the step signs
    only the archives that the job output names."""
    run = Fetch(tmp_path, SIGN_RUN, files, digests)

    assert run.done.returncode == 1
    assert fault in run.done.stderr
    assert run.fetches == 0
    assert not (run.bundles / "SHA256SUMS").exists()


@needs_sha256sum_check
def test_the_sign_step_refuses_an_archive_with_other_bytes(tmp_path: Path) -> None:
    run = Fetch(tmp_path, SIGN_RUN, {ONE: BYTES[ONE] + b"!"}, LINES[ONE])

    assert run.done.returncode != 0
    assert run.fetches == 0
    assert not run.used.exists()


def test_with_no_archive_the_cosign_step_of_the_release_fetches_nothing(tmp_path: Path) -> None:
    """A run with no binary component fetches no program of 141 MB. The tag
    script reads COSIGN only for a binary component."""
    run = Fetch(tmp_path, COSIGN_RUN, {"SHA256SUMS": b""})

    assert run.done.returncode == 0, run.done.stdout + run.done.stderr
    assert run.done.stdout == "cosign: no archive, so no fetch\n"
    assert run.fetches == 0
    assert not run.program.exists()


@pytest.mark.skipif(
    not IN_CI and shutil.which("sha256sum") is None, reason="the step uses sha256sum"
)
@pytest.mark.parametrize("name", [ONE, *DISK_NAMES])
def test_the_cosign_fetch_runs_for_any_named_asset(tmp_path: Path, name: str) -> None:
    """The release job takes cosign when the directory holds an archive or
    any one file of a disk-image row. A run that ships disks alone then has
    the program that the tag script needs. The sign job holds the same
    text (`test_the_sign_job_and_the_release_job_take_one_cosign`)."""
    files = {name: b"a signed file", f"{name}{BUNDLE_SUFFIX}": b"{}", "SHA256SUMS": b""}
    run = Fetch(tmp_path, COSIGN_RUN, files)

    assert run.done.returncode == 0, run.done.stdout + run.done.stderr
    assert run.fetches == 1


def test_a_bundle_alone_fetches_no_cosign(tmp_path: Path) -> None:
    """A bundle names no signed file: the glob holds the names of the files
    alone."""
    files = {f"{name}{BUNDLE_SUFFIX}": b"{}" for name in [ONE, *DISK_NAMES]}
    run = Fetch(tmp_path, COSIGN_RUN, {**files, "SHA256SUMS": b""})

    assert run.done.returncode == 0, run.done.stdout + run.done.stderr
    assert run.done.stdout == "cosign: no archive, so no fetch\n"
    assert run.fetches == 0


@pytest.mark.skipif(
    not IN_CI and shutil.which("sha256sum") is None, reason="the step uses sha256sum"
)
def test_the_cosign_step_runs_the_program_only_after_the_compare(tmp_path: Path) -> None:
    run = Fetch(tmp_path, COSIGN_RUN, {ONE: BYTES[ONE]})

    assert run.done.returncode == 0, run.done.stdout + run.done.stderr
    assert run.fetches == 1
    assert run.used.read_text(encoding="utf-8") == "version\n"
    assert os.access(run.program, os.X_OK)


@pytest.mark.skipif(
    not IN_CI and shutil.which("sha256sum") is None, reason="the step uses sha256sum"
)
def test_the_cosign_step_never_runs_a_file_with_another_sha256(tmp_path: Path) -> None:
    run = Fetch(tmp_path, COSIGN_RUN, {ONE: BYTES[ONE]}, sha256=COSIGN_ENV["COSIGN_SHA256"])

    assert run.done.returncode == 1
    assert f"not {COSIGN_ENV['COSIGN_SHA256']}" in run.done.stderr
    assert not run.used.exists()
    assert not os.access(run.program, os.X_OK)


@pytest.mark.parametrize(
    ("workflow", "name"), [(GATE, GATE_NAME), (RELEASE, RELEASE_NAME)], ids=BY_NAME
)
def test_a_workflow_file_has_no_key_that_changes_each_job(
    workflow: dict[Any, Any], name: str
) -> None:
    """A job that is equal in the two files runs the same steps only when no
    key beside `jobs` changes them. A `defaults` key of one file gives each
    `run` step another shell, and an `env` key gives each step a variable.
    Only the gate has a `concurrency` key. Only the release has an `env`
    key, and it holds the pin of cosign and nothing else."""
    assert set(workflow) == FILE_KEYS[name]
    assert workflow.get("env", COSIGN_ENV) == COSIGN_ENV


def _node_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if step.get("uses", "").startswith(NODE_ACTIONS)]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_proc_job_builds_the_facade_then_runs_the_process_suite(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The suite starts the built facade, so the build comes first, and the
    venv comes before the suite. The suite is one command, with no path of
    one test and no `-k`."""
    proc = jobs["proc"]
    runs = [step for step in proc["steps"] if "run" in step]
    facade, suite = runs
    order = [step.get("uses") or step["run"] for step in proc["steps"]]

    assert proc["needs"] == "scope"
    assert proc["if"] == ONLY_CODE
    assert facade == {"run": FACADE_BUILD, "working-directory": FACADE_BUILD_DIR}
    assert suite["run"] == PROC_RUN
    assert order.index(FACADE_BUILD) < order.index(UV_SYNC) < order.index(PROC_RUN)


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_proc_job_has_node_and_pnpm_as_the_facade_job_has_them(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """One node version and one pnpm for the facade, whichever job builds
    it. The suite must judge the build that the `facade` job tests."""
    steps = _node_steps(jobs["proc"])

    assert [step["uses"].split("@")[0] for step in steps] == [
        name.rstrip("@") for name in NODE_ACTIONS
    ]
    assert steps == _node_steps(jobs[FACADE_JOB])


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_agent_job_tests_and_typechecks_the_module(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The Python gate never runs the TypeScript suite of `agent/`. A run
    line that drops `pnpm test` or the type check is green and judges
    nothing."""
    runs = [step for step in jobs[AGENT_JOB]["steps"] if "run" in step]

    assert [(step["run"], step["working-directory"]) for step in runs] == [(AGENT_RUN, AGENT_DIR)]
    assert [one["uses"].split("@")[0] for one in _node_steps(jobs[AGENT_JOB])] == [
        name.rstrip("@") for name in NODE_ACTIONS
    ]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_facade_job_tests_and_typechecks_the_facade(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The Python gate never runs the TypeScript suite of `facade/`. A run
    line that drops `pnpm test` or the type check is green and judges
    nothing."""
    runs = [step for step in jobs[FACADE_JOB]["steps"] if "run" in step]

    assert [(step["run"], step["working-directory"]) for step in runs] == [(FACADE_RUN, FACADE_DIR)]
    assert [one["uses"].split("@")[0] for one in _node_steps(jobs[FACADE_JOB])] == [
        name.rstrip("@") for name in NODE_ACTIONS
    ]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_playpen_job_builds_the_published_image(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    job = jobs[PLAYPEN_JOB]
    checkout, build = job["steps"]

    assert job["needs"] == "scope"
    assert job["if"] == ONLY_CODE
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 20
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"fetch-depth": 0}
    assert build == {"run": PLAYPEN_RUN}


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_rust_job_formats_lints_and_tests_the_workspace(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The toolchain step installs what rust/rust-toolchain.toml names. The
    frozen host API step diffs the frozen files against the base. The three
    checks then run in rust/, and a lint warning fails the job."""
    job = jobs[RUST_JOB]
    checkout, toolchain, frozen, *checks = job["steps"]

    assert job["needs"] == "scope"
    assert job["if"] == ONLY_CODE
    assert job["env"] == {"CARGO_INCREMENTAL": 0}
    assert checkout["with"] == {"fetch-depth": 0}
    assert toolchain["working-directory"] == RUST_DIR
    assert "rustup toolchain install --no-self-update" in toolchain["run"]
    assert frozen["name"] == FROZEN_STEP
    assert frozen["env"] == FROZEN_ENV
    assert frozen["run"].startswith("set -euo pipefail\n")
    assert frozen["run"].endswith(FROZEN_DIFF + "\n")
    assert checks == [{"run": run, "working-directory": RUST_DIR} for run in RUST_RUNS]


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_kvm_job_readies_the_runner_then_runs_every_kvm_test(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The setup script runs first, on the runner itself, and builds CH
    and the test programs. The test run reads the same directory as the
    script, and no step has a rule or `continue-on-error`: no KVM is a
    red job. A run that matches no kvm_ test is red too, because the
    count step fails first."""
    job = jobs[KVM_JOB]
    checkout, setup, toolchain, count, run = job["steps"]

    assert set(job) == {"needs", "if", "runs-on", "timeout-minutes", "env", "steps"}
    assert job["needs"] == "scope"
    assert job["if"] == ONLY_CODE
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["env"] == {"CARGO_INCREMENTAL": 0}
    assert checkout["with"] == {"fetch-depth": 0}
    assert setup == {"run": KVM_SETUP, "env": KVM_ENV}
    assert (REPO / KVM_SETUP).is_file()
    assert toolchain["working-directory"] == RUST_DIR
    assert "rustup toolchain install --no-self-update" in toolchain["run"]
    assert count == {"name": "count the kvm tests", "working-directory": RUST_DIR, "run": KVM_COUNT}
    assert run == {"run": KVM_RUN, "working-directory": RUST_DIR, "env": KVM_ENV}


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_memory_job_checks_the_package_in_its_own_venv(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The memory package is not a workspace member, so no shard and no lint
    run sees its store code. Its job syncs its own lock, then runs pyright
    and pytest there."""
    steps = jobs[MEMORY_JOB]["steps"]
    runs = [step for step in steps if "run" in step]

    assert jobs[MEMORY_JOB]["if"] == ONLY_CODE
    assert steps[0]["with"] == {"fetch-depth": 0}
    assert [step["run"] for step in runs] == MEMORY_RUNS
    assert {step["working-directory"] for step in runs} == {MEMORY_DIR}
    assert UV_SYNC not in [step.get("uses") for step in steps]


def test_pyright_in_the_memory_job_reads_the_config_of_the_package() -> None:
    """pyright looks for a `pyrightconfig.json` in each parent folder before
    it reads `[tool.pyright]` of `pyproject.toml`. With no such file in
    `memory/`, a run there loads the root file and checks the whole
    workspace, which the memory venv cannot import."""
    config = json.loads((REPO / MEMORY_DIR / "pyrightconfig.json").read_text(encoding="utf-8"))
    package = tomllib.loads((REPO / MEMORY_DIR / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["include"] == ["src"]
    assert "pyright" not in package.get("tool", {})


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_a_skip_in_the_proc_job_is_a_failure(jobs: dict[str, dict[str, Any]], last: str) -> None:
    """A test that starts the facade skips itself when the build is missing. The
    switch must be one that the suite reads: the suite stops on a variable
    with its prefix that it does not know, and a name with another prefix
    would change nothing."""
    (suite,) = [step for step in jobs["proc"]["steps"] if step.get("run") == PROC_RUN]

    assert suite["env"] == PROC_ENV
    for name in PROC_ENV:
        assert f'"{name}"' in PROC_SWITCHES.read_text(encoding="utf-8")


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_proc_job_has_a_time_limit(jobs: dict[str, dict[str, Any]], last: str) -> None:
    """A job with no limit has six hours. Each test of the suite has 180 s, so
    the job needs no more than its own run and a cold build."""
    assert jobs["proc"]["timeout-minutes"] == PROC_MINUTES


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_proc_job_keeps_the_pinned_chaperone_in_a_cache(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """One cache step, pinned by a commit, after the venv and before the
    suite. Its key holds the pin file, so a new pin builds again. Its path
    is the cache that the build of the suite writes."""
    steps = jobs["proc"]["steps"]
    (cache,) = [step for step in steps if _is_cache(step)]
    order = [step.get("uses") or step["run"] for step in steps]
    sha = cache["uses"].removeprefix(CACHE_ACTION).split()[0]

    assert len(sha) == 40 and all(char in "0123456789abcdef" for char in sha)
    assert cache["with"] == CHAPERONE_CACHE
    assert order.index(UV_SYNC) < steps.index(cache) < order.index(PROC_RUN)
    assert CHAPERONE_PIN.is_file()
    assert '"creche-proc" / "chaperone-rs"' in CHAPERONE_BUILD.read_text(encoding="utf-8")


def _is_cache(step: dict[str, Any]) -> bool:
    return str(step.get("uses", "")).startswith(CACHE_ACTION)


def _is_facade_build(step: dict[str, Any]) -> bool:
    return step.get("working-directory") == FACADE_BUILD_DIR


def _report_of(name: str) -> str:
    """The path of the JUnit report of one suite, as the workflow spells it."""
    return f"{REPORT_DIR}/{name}.xml"


def _setup_of(job: dict[str, Any], run: str) -> list[dict[str, Any]]:
    """The steps of a job before the step whose command is `run`."""
    runs = [step.get("run") for step in job["steps"]]

    return job["steps"][: runs.index(run)]


def _suite_steps(jobs: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The steps of the `suites` job of one workflow after its setup: one for
    each suite, and the last one."""
    suites = jobs[SUITES_JOB]
    first = next(iter(SUITE_RUNS.values()))
    *runs, last = suites["steps"][len(_setup_of(suites, first)) :]

    return runs, last


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_suites_job_has_the_checkout_and_the_venv_of_the_proc_job(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """Each step before the first suite is a step of the `proc` job, in the
    same order: the checkout, then the venv. No suite of this job starts
    node, so the job has no node, no pnpm and no build. The `proc` job has
    those steps and the cache of the chaperone."""
    suites = jobs[SUITES_JOB]
    setup = _setup_of(suites, next(iter(SUITE_RUNS.values())))

    assert set(suites) == SUITES_KEYS
    assert suites["needs"] == "scope"
    assert suites["if"] == ONLY_CODE
    assert suites["runs-on"] == jobs["proc"]["runs-on"]
    proc_setup = _setup_of(jobs["proc"], PROC_RUN)
    node = _node_steps(jobs["proc"])
    shared = [
        step
        for step in proc_setup
        if not _is_cache(step) and not _is_facade_build(step) and step not in node
    ]

    assert setup == shared
    assert _node_steps(suites) == []
    assert [step.get("uses") or step["run"] for step in setup][-1] == UV_SYNC


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_suites_job_runs_each_suite_in_a_pytest_run_of_its_own(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """After the setup the job has one step for each suite, then the last
    step. A suite step holds the whole command of `integration/AGENTS.md`,
    with no path of one test and no `-k`. Its one other key is the variable
    that names its report."""
    runs, no_skip = _suite_steps(jobs)

    assert len(runs) == len(SUITE_RUNS)
    for step, (name, run) in zip(runs, SUITE_RUNS.items(), strict=True):
        assert step == {"run": run, "env": {REPORT_VARIABLE: REPORT_OPTION + _report_of(name)}}
    assert no_skip["name"] == NO_SKIP_STEP


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_last_step_of_the_suites_job_reads_the_report_of_each_suite(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """The step gets one path for each suite, on a line of its own. A path
    that differs from the path of a run would judge a report that no run
    wrote. No `if` and no `continue-on-error` can hold the step back."""
    _, no_skip = _suite_steps(jobs)

    assert set(no_skip) == NO_SKIP_KEYS
    assert no_skip["shell"] == NO_SKIP_SHELL
    assert no_skip["env"] == {REPORTS_VARIABLE: "\n".join(_report_of(name) for name in SUITE_RUNS)}


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_suites_job_has_a_time_limit(jobs: dict[str, dict[str, Any]], last: str) -> None:
    """The stack of a test waits for each listener and for each child at its
    teardown. A fault there can cost each test that wait."""
    assert jobs[SUITES_JOB]["timeout-minutes"] == SUITES_MINUTES


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """A JUnit report for each kind of run in `SMALL_RUNS`, from the pytest of
    this workspace, plus the two reports that are not one. The last step must
    read the form that this pytest writes."""
    root = tmp_path_factory.mktemp("reports")
    env = {name: value for name, value in os.environ.items() if name != REPORT_VARIABLE}
    found = {ABSENT_REPORT: root / "absent.xml", BROKEN_REPORT: root / "broken.xml"}
    found[BROKEN_REPORT].write_text("<testsuites>", encoding="utf-8")

    for name, body in SMALL_RUNS.items():
        home = root / name
        home.mkdir()
        (home / "test_small.py").write_text(body, encoding="utf-8")
        found[name] = root / f"{name}.xml"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                f"--rootdir={home}",
                f"--confcutdir={home}",
                f"{REPORT_OPTION}{found[name]}",
                "test_small.py",
            ],
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert found[name].is_file(), f"the run `{name}` wrote no report"

    return found


def _no_skip(paths: list[Path]) -> subprocess.CompletedProcess[str]:
    """Runs the last step of the `suites` job over `paths`. The step is the
    one of `gate.yml`. `test_the_release_runs_the_gates_test_jobs` holds the
    job of `release.yml` equal to that job, so one run judges both copies."""
    _, last = _suite_steps(JOBS)

    return subprocess.run(
        [sys.executable, "-c", last["run"]],
        env={REPORTS_VARIABLE: "\n".join(str(path) for path in paths)},
        capture_output=True,
        text=True,
        timeout=30,
    )


#: (the report of each suite, whether the last step is green)
NO_SKIP_VERDICTS: list[tuple[list[str], bool]] = [
    (["ran", "ran"], True),
    # One test that skips is red, in each of the two places.
    (["ran", "one_skipped"], False),
    (["one_skipped", "ran"], False),
    # A suite in which each test skips is the run that judged nothing.
    (["ran", "each_skipped"], False),
    (["ran", "no_test"], False),
    # A suite with no report did not run, or it wrote to another path.
    (["ran", ABSENT_REPORT], False),
    (["ran", BROKEN_REPORT], False),
    ([], False),
]


@pytest.mark.parametrize(("kinds", "green"), NO_SKIP_VERDICTS, ids=str)
def test_the_suites_job_is_green_only_when_no_test_skipped(
    reports: dict[str, Path], kinds: list[str], green: bool
) -> None:
    done = _no_skip([reports[kind] for kind in kinds])

    assert (done.returncode == 0) == green, done.stdout + done.stderr


def test_the_last_step_prints_the_counts_of_each_suite(reports: dict[str, Path]) -> None:
    """The log of a run shows how many tests ran and how many skipped, for
    each suite. A reader counts the green runs of the job from those lines."""
    done = _no_skip([reports["ran"], reports["one_skipped"], reports["each_skipped"]])

    assert "ran: 1 of 1 tests ran, 0 skipped" in done.stdout
    assert "one_skipped: 1 of 2 tests ran, 1 skipped" in done.stdout
    assert "each_skipped: 0 of 2 tests ran, 2 skipped" in done.stdout
    assert "::error::one_skipped: 1 of 2 tests skipped" in done.stdout
    assert "::error::each_skipped: the suite ran no test" in done.stdout


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_a_change_outside_systemd_skips_the_proof_step_and_not_the_job(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """A job skipped as a whole on a code change is red in the verdict. So
    the job runs, and each step after the checkout carries the rule."""
    checkout, *steps = jobs[SYSTEMD_JOB]["steps"]

    assert jobs["scope"]["outputs"]["systemd"] == SYSTEMD_OUTPUT
    assert jobs[SYSTEMD_JOB]["needs"] == "scope"
    assert jobs[SYSTEMD_JOB]["if"] == ONLY_CODE
    assert checkout["uses"].startswith("actions/checkout@")
    assert "if" not in checkout
    assert steps, "the systemd-proof job has no step but the checkout"
    for step in steps:
        assert step.get("if") == ONLY_SYSTEMD, f"{step.get('name') or step.get('run')} always runs"


@pytest.mark.parametrize(("jobs", "last"), WORKFLOW_JOBS, ids=BY_NAME)
def test_the_systemd_job_runs_the_proof_on_the_runner_itself(
    jobs: dict[str, dict[str, Any]], last: str
) -> None:
    """One copy of the proof: `bin/systemd-proof.sh`. The job has no
    container, because a container has no systemd as process 1. The test
    holds each key of the job and each key of the step."""
    job = jobs[SYSTEMD_JOB]
    _checkout, proof = job["steps"]

    assert set(job) == SYSTEMD_KEYS
    assert job["runs-on"] == SYSTEMD_RUNNER
    assert 0 < job["timeout-minutes"] <= 15
    assert set(proof) == SYSTEMD_STEP_KEYS
    assert proof["run"] == SYSTEMD_RUN
    assert (REPO / SYSTEMD_RUN).is_file()


def _scope_run(name: str) -> str:
    """The script of the step that answers the scope of a workflow."""
    if name == GATE_NAME:
        (step,) = SCOPE_ACTION["runs"]["steps"]
    else:
        (step,) = [one for one in RELEASE_JOBS["scope"]["steps"] if one.get("id") == "scope"]

    return step["run"]


@pytest.mark.parametrize("name", BY_NAME)
def test_a_scope_skips_the_proof_only_when_the_script_says_unchanged(name: str) -> None:
    """The release also asks for a commit whose own run passed, as it does
    for `docs`: a push never skips the proof over a tree whose proof failed,
    or is still in, its run."""
    run = _scope_run(name)

    assert run.count("systemd=true\n") == 1
    assert run.count("systemd=false\n") == 1
    assert SYSTEMD_ASKED[name] in run
    assert run.index("systemd=true\n") < run.index(SYSTEMD_ASKED[name])
    assert 'echo "systemd=$systemd" >> "$GITHUB_OUTPUT"' in run


def test_the_scope_action_gives_the_systemd_answer_as_an_output() -> None:
    assert SCOPE_ACTION["outputs"]["systemd"]["value"] == SYSTEMD_OUTPUT


def test_the_tag_step_runs_only_after_the_verdict() -> None:
    steps = RELEASE_JOBS[RELEASE_NAME]["steps"]
    names = [step.get("name") for step in steps]
    verdict = _step(RELEASE_JOBS, RELEASE_NAME, VERDICT_STEP)
    tag = _step(RELEASE_JOBS, RELEASE_NAME, TAG_STEP)

    assert verdict["uses"] == VERDICT
    assert verdict["with"] == VERDICT_WITH
    assert names.index(VERDICT_STEP) < names.index(TAG_STEP)

    # No `if` on any step: a failed verdict skips every step after it, and
    # the job is `always()`, so a rule on a step is the one way to tag a red
    # tree.
    for step in steps:
        assert "if" not in step, f"{step.get('name') or step.get('uses')} sets its own rule"

    assert tag["run"] == TAG_RUN


def test_no_time_limit_can_stop_an_allocation_half_way() -> None:
    """A time limit on the `release` job or on its tag step can stop the
    tag script between a tag and its Release."""
    tag = _step(RELEASE_JOBS, RELEASE_NAME, TAG_STEP)

    assert "timeout-minutes" not in RELEASE_JOBS[RELEASE_NAME]
    assert "timeout-minutes" not in tag


def test_the_tag_step_says_which_catalog_repository_it_tags() -> None:
    """The allocator would otherwise take the name from where the
    repository is hosted, and refuse a name the catalog does not hold
    (`handover/bin/allocate-tags.sh`, TAG_REPO). The step also gets the
    assets of a binary component and the program that checks them."""
    tag = _step(RELEASE_JOBS, RELEASE_NAME, TAG_STEP)

    assert tag["env"]["TAG_REPO"] == THIS_REPOSITORY
    assert tag["env"] == TAG_ENV


def test_two_merges_never_allocate_at_once() -> None:
    """Two runs that read the same newest tag would allocate one number
    twice, and a run stopped half way would leave a tag with no Release."""
    assert RELEASE_JOBS[RELEASE_NAME]["concurrency"] == ALLOCATE_ONE_AT_A_TIME

    # A group on the whole workflow would queue the suite too, and a queued
    # run that gives way would never test its commit.
    assert "concurrency" not in RELEASE


def test_only_the_release_job_may_write() -> None:
    """Every other job runs the merged code, so none of them holds the
    token that tags. Three rules:

    1. The `release` job holds the token that tags.
    2. The `sign` job holds the identity token and no other permission. It
       has no checkout and no local action, so no code of the repository
       runs beside that token.
    3. No other job sets its permissions."""
    assert RELEASE["permissions"] == {"contents": "read", "actions": "read"}
    assert RELEASE_JOBS[RELEASE_NAME]["permissions"] == {
        "contents": "write",
        "pull-requests": "read",
    }
    assert RELEASE_JOBS[SIGN_JOB]["permissions"] == SIGN_PERMISSIONS
    for step in RELEASE_JOBS[SIGN_JOB]["steps"]:
        uses = step.get("uses", "")

        assert not uses.startswith("actions/checkout@"), "the sign job has a checkout"
        assert not uses.startswith("./"), f"the sign job uses the local action {uses}"
    for name, job in RELEASE_JOBS.items():
        if name not in (RELEASE_NAME, SIGN_JOB):
            assert "permissions" not in job, f"{name} sets its own permissions"
    for name, job in JOBS.items():
        assert "permissions" not in job, f"{name} sets its own permissions"


def test_gate_judges_with_the_shared_verdict_and_nothing_else() -> None:
    """A pull request carries no number, so no step of `gate` reads one. The
    job is `always()` and no `if` holds its one check back."""
    gate = JOBS[GATE_NAME]
    runs = [step for step in gate["steps"] if "run" in step]

    assert runs == []
    assert "if" not in _gate_step(VERDICT_STEP)
    assert _gate_step(VERDICT_STEP)["uses"] == VERDICT
    assert _gate_step(VERDICT_STEP)["with"] == VERDICT_WITH


def test_no_file_carries_a_version_to_bump() -> None:
    """Contract 06 §2.1: CI allocates each component's number at the merge.
    A root version that moved again would bring back the collision of two
    pull requests that chose the same one."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    assert root["project"]["version"] == "0.0.0"


@pytest.mark.parametrize("total", [1, 2, 6, 13])
def test_every_test_is_in_exactly_one_shard(total: int) -> None:
    in_shard = _root_conftest().in_shard
    ids = [f"chaperone/tests/test_{number}.py::test_it[{number}]" for number in range(500)]

    for one in ids:
        holders = [shard for shard in range(1, total + 1) if in_shard(one, shard, total)]
        assert len(holders) == 1, f"{one} is in shards {holders} of {total}"


def test_the_shards_of_a_real_suite_add_up_to_the_suite() -> None:
    total = 3
    whole = _collected(SMALL_SUITE)
    shards = [_collected(SMALL_SUITE, "--shard", f"{k}/{total}") for k in range(1, total + 1)]

    assert whole, f"{SMALL_SUITE} collects nothing"
    assert sorted(one for shard in shards for one in shard) == sorted(whole)


@pytest.mark.parametrize("text", ["0/4", "5/4", "4", "a/b", "1/0", "/"])
def test_a_shard_outside_one_to_n_is_refused(text: str) -> None:
    with pytest.raises(pytest.UsageError):
        _root_conftest().parse_shard(text)


def _needs(
    jobs: dict[str, dict[str, Any]], last: str, scope: str | None, results: dict[str, str]
) -> str:
    """`toJSON(needs)` as the last job of `jobs` sees it: every job a success
    but for the ones `results` names. A name that the file does not hold is
    left out. `test_the_last_job_needs_every_other_job_and_always_runs` holds
    that the last job needs each other job."""
    names = sorted(set(jobs) - {last})
    needs = {name: {"result": results.get(name, "success"), "outputs": {}} for name in names}
    if scope is not None:
        needs["scope"]["outputs"] = {"scope": scope}

    return json.dumps(needs)


#: What a docs PR skips. A code PR skips nothing.
DOCS = {
    "tests": "skipped",
    AGENT_JOB: "skipped",
    FACADE_JOB: "skipped",
    "proc": "skipped",
    SUITES_JOB: "skipped",
    MEMORY_JOB: "skipped",
    PLAYPEN_JOB: "skipped",
    RUST_JOB: "skipped",
    SYSTEMD_JOB: "skipped",
    KVM_JOB: "skipped",
    VMM_ARCHIVE_JOB: "skipped",
}

#: (the scope, what the jobs did, whether the last job is green), for the
#: last job of each of the two files.
CASES: list[tuple[str | None, dict[str, str], bool]] = [
    ("code", {}, True),
    ("docs", DOCS, True),
    # One red shard makes the matrix job a failure.
    ("code", {"tests": "failure"}, False),
    ("code", {"proc": "cancelled"}, False),
    ("code", {AGENT_JOB: "failure"}, False),
    ("code", {AGENT_JOB: "skipped"}, False),
    ("docs", DOCS | {AGENT_JOB: "success"}, False),
    ("code", {FACADE_JOB: "failure"}, False),
    ("code", {FACADE_JOB: "skipped"}, False),
    ("docs", DOCS | {FACADE_JOB: "success"}, False),
    ("code", {"proc": "failure"}, False),
    ("code", {SYSTEMD_JOB: "failure"}, False),
    ("code", {SYSTEMD_JOB: "cancelled"}, False),
    ("code", {PLAYPEN_JOB: "failure"}, False),
    ("code", {PLAYPEN_JOB: "skipped"}, False),
    # No KVM on the runner fails the job. It never skips on a code PR.
    ("code", {KVM_JOB: "failure"}, False),
    ("code", {KVM_JOB: "skipped"}, False),
    ("docs", DOCS | {KVM_JOB: "success"}, False),
    ("code", {"lint": "failure"}, False),
    ("docs", DOCS | {"lint": "failure"}, False),
    # A suite that did not run on a code PR is red, not skipped.
    ("code", {"tests": "skipped"}, False),
    ("code", {"lint": "skipped"}, False),
    # The process suite is in no shard. A code PR on which it did not run
    # is red.
    ("code", {"proc": "skipped"}, False),
    # The old suite of integration/ is in no shard either.
    ("code", {SUITES_JOB: "failure"}, False),
    ("code", {SUITES_JOB: "skipped"}, False),
    # The systemd proof runs on every code PR. The job skips its own step
    # when the PR touches no file of the proof, and is a success.
    ("code", {SYSTEMD_JOB: "skipped"}, False),
    # A job that ran on a docs PR is not what the scope asks for.
    ("docs", DOCS | {"proc": "success"}, False),
    ("docs", DOCS | {SUITES_JOB: "success"}, False),
    ("docs", DOCS | {SYSTEMD_JOB: "success"}, False),
    # No scope: the scope job failed and everything behind it was skipped.
    (None, DOCS | {"scope": "failure"}, False),
]

#: The cases of the job that only the gate has. It runs on every code PR,
#: and skips its own steps when the PR touches no file of the vmm build.
GATE_CASES: list[tuple[str | None, dict[str, str], bool]] = [
    ("code", {VMM_ARCHIVE_JOB: "failure"}, False),
    ("code", {VMM_ARCHIVE_JOB: "skipped"}, False),
    ("docs", DOCS | {VMM_ARCHIVE_JOB: "success"}, False),
]

#: The cases of the jobs that only the release has. Each one runs in each
#: scope, so a failed or skipped one holds back each tag of the run.
RELEASE_CASES: list[tuple[str | None, dict[str, str], bool]] = [
    ("code", {ARTIFACTS_JOB: "failure"}, False),
    ("code", {ARTIFACTS_JOB: "skipped"}, False),
    ("docs", DOCS | {ARTIFACTS_JOB: "success"}, True),
    ("docs", DOCS | {ARTIFACTS_JOB: "skipped"}, False),
    # A failed signature holds back each tag of the run.
    ("code", {SIGN_JOB: "failure"}, False),
    ("code", {SIGN_JOB: "skipped"}, False),
    ("docs", DOCS | {SIGN_JOB: "cancelled"}, False),
    ("docs", DOCS | {SIGN_JOB: "success"}, True),
    # No plan: the archives of the run are not known.
    ("code", {PLAN_JOB: "failure"}, False),
    ("code", {PLAN_JOB: "skipped"}, False),
    ("docs", DOCS | {PLAN_JOB: "success"}, True),
]

#: (what the jobs did, whether the last job is green)
VERDICTS = [
    *[
        (_needs(jobs, last, scope, results), green)
        for jobs, last in WORKFLOW_JOBS
        for scope, results, green in CASES
    ],
    *[
        (_needs(RELEASE_JOBS, RELEASE_NAME, scope, results), green)
        for scope, results, green in RELEASE_CASES
    ],
    *[(_needs(JOBS, GATE_NAME, scope, results), green) for scope, results, green in GATE_CASES],
]


@pytest.mark.parametrize(("needs", "green"), VERDICTS)
def test_the_verdict_is_green_only_when_every_job_its_scope_asks_for_passed(
    needs: str, green: bool
) -> None:
    (step,) = VERDICT_ACTION["runs"]["steps"]
    assert step["env"] == {"NEEDS": "${{ inputs.needs }}"}

    done = subprocess.run(
        [sys.executable, "-c", step["run"]],
        env={"NEEDS": needs},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert (done.returncode == 0) == green, done.stdout + done.stderr


#: What the scope action answers for a change, as (docs or code, systemd).
SCOPES = [
    (["chaperone/src/chaperone/app.py"], ("code", "false")),
    (["docs/later.md"], ("docs", "false")),
    # The CI files are no files of the systemd proof: the tests of this file
    # hold the `systemd-proof` job.
    ([".github/workflows/gate.yml"], ("code", "false")),
    ([".github/workflows/release.yml"], ("code", "false")),
    ([".github/actions/scope/action.yml"], ("code", "false")),
    (["vectors/data/index.json"], ("code", "false")),
    (["vectors/README.md"], ("docs", "false")),
    (["bin/quality-gate.sh"], ("code", "false")),
    ([".github/actions/verdict/action.yml"], ("code", "false")),
    # A unit file, and the proof script itself. The tests of the script use
    # a fake systemd, so only this run proves the change with the real one.
    (["systemd/creche-attendance.service"], ("code", "true")),
    (["chaperone/src/chaperone/app.py", "systemd/creche-follow.timer"], ("code", "true")),
    (["bin/systemd-proof.sh"], ("code", "true")),
    (["systemd/creche-new@.timer"], ("code", "true")),
    # git writes this path inside double quotes.
    (['systemd/creche-"one".service'], ("code", "true")),
    # Every path under systemd/ counts, a Markdown file too. The
    # `systemd-proof` job is left out of a docs PR, whatever this answer is.
    (["systemd/AGENTS.md"], ("docs", "true")),
    # Only the directory at the root holds the unit files.
    (["docs/systemd/notes.md"], ("docs", "false")),
    (["systemd-notes.txt"], ("code", "false")),
    (["handover/src/handover/catalog.py"], ("code", "false")),
    (["handover/AGENTS.md"], ("docs", "false")),
]


def _git(repo: Path, *args: str) -> str:
    leaked = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    )
    env = {name: value for name, value in os.environ.items() if name not in leaked}
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        env=env | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    return done.stdout.strip()


def _scope_of(repo: Path, base: str) -> tuple[str, str]:
    """The docs answer and the systemd answer of the scope action."""
    outputs = _scope_outputs(repo, base)

    return outputs["scope"], outputs["systemd"]


def _scope_outputs(repo: Path, base: str) -> dict[str, str]:
    """Runs the scope action's own script in `repo`, as a merge group on
    `base` would. Returns its three outputs."""
    (step,) = SCOPE_ACTION["runs"]["steps"]
    output = repo.parent / "output"
    output.write_text("", encoding="utf-8")
    done = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=repo,
        env={
            "PATH": os.environ["PATH"],
            "GROUP_BASE": base,
            "GITHUB_BASE_REF": "main",
            "GITHUB_OUTPUT": str(output),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())

    assert set(outputs) == {"scope", "systemd", "vmm"}

    return outputs


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository with one commit, the rule the scope action sources and
    the script that it asks."""
    repo = tmp_path / "repo"
    (repo / "bin" / "lib").mkdir(parents=True)
    for name in ("lib/docsrule.sh", "systemd-proof.sh"):
        shutil.copy2(REPO / "bin" / name, repo / "bin" / name)

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the base")

    return repo


@pytest.mark.parametrize(("paths", "scope"), SCOPES, ids=lambda one: " ".join(one))
def test_the_scope_says_whether_a_change_is_docs_and_touches_systemd(
    checkout: Path, paths: list[str], scope: tuple[str, str]
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    for name in paths:
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        # One more line, not a new body: the scope sources the rule and runs
        # the script.
        with (checkout / name).open("a", encoding="utf-8") as file:
            file.write("# changed\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "the change")

    assert _scope_of(checkout, base) == scope


def test_a_unit_file_that_moves_out_of_systemd_runs_the_proof(checkout: Path) -> None:
    """git can report a move as one change with the new path only. The scope
    must see the old path too: the proof then reads one unit file less."""
    (checkout / "systemd").mkdir()
    (checkout / "systemd" / "creche-one.service").write_text(
        "[Service]\nExecStart=/bin/true\n", encoding="utf-8"
    )
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "a unit")
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "units").mkdir()
    _git(checkout, "mv", "systemd/creche-one.service", "units/creche-one.service")
    _git(checkout, "commit", "-q", "-m", "the move")

    assert _scope_of(checkout, base) == ("code", "true")


def test_a_change_the_scope_cannot_read_is_code_and_systemd(checkout: Path) -> None:
    """No merge group, and no `origin/main` to take a merge-base with: the
    full suite and the systemd proof are the safe answer."""
    assert _scope_of(checkout, "") == ("code", "true")


def test_a_base_that_the_clone_lacks_is_code_and_systemd(checkout: Path) -> None:
    """A merge group names its base. A clone that lacks that commit reads no
    change, and each check is then the safe answer."""
    assert _scope_of(checkout, "0" * 40) == ("code", "true")


#: What the scope action answers for the vmm build, for a change of these
#: paths (decision CH76 rule 4).
VMM_SCOPES = [
    (["rust/crates/vmm/src/main.rs"], "true"),
    (["rust/Cargo.lock"], "true"),
    (["rust/component.toml"], "true"),
    (["rust/crates/vmm/AGENTS.md"], "true"),
    (["bin/lib/chbuild.sh"], "true"),
    (["bin/build-artifacts.sh"], "true"),
    (["bin/lib/artifactrows.sh"], "true"),
    (["bin/lib/elfstatic.sh"], "true"),
    (["handover/src/handover/catalog.py"], "false"),
    (["bin/vm-smoke-setup.sh"], "false"),
    (["docs/rust/notes.md"], "false"),
    (["rust-notes.txt"], "false"),
]


@pytest.mark.parametrize(("paths", "vmm"), VMM_SCOPES, ids=lambda one: " ".join(one))
def test_the_scope_says_whether_a_change_touches_the_vmm_build(
    checkout: Path, paths: list[str], vmm: str
) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    for name in paths:
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        with (checkout / name).open("a", encoding="utf-8") as file:
            file.write("# changed\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "the change")

    assert _scope_outputs(checkout, base)["vmm"] == vmm


@pytest.mark.parametrize("base", ["", "0" * 40], ids=["none", "absent"])
def test_a_base_the_scope_cannot_read_builds_the_vmm(checkout: Path, base: str) -> None:
    """No base, or one that the clone lacks: the build is the safe answer."""
    assert _scope_outputs(checkout, base)["vmm"] == "true"


def test_a_file_that_moves_out_of_rust_builds_the_vmm(checkout: Path) -> None:
    """A move is a change of the old path too."""
    (checkout / "rust").mkdir()
    (checkout / "rust" / "one.rs").write_text("fn one() {}\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "a crate file")
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "other").mkdir()
    _git(checkout, "mv", "rust/one.rs", "other/one.rs")
    _git(checkout, "commit", "-q", "-m", "the move")

    assert _scope_outputs(checkout, base)["vmm"] == "true"


@pytest.mark.parametrize("fault", ["absent", "not executable", "fails"])
def test_a_scope_that_gets_no_answer_from_the_script_runs_the_proof(
    checkout: Path, fault: str
) -> None:
    """The change touches no file of the proof, so the script of the commit
    says `unchanged`. A script that gives no answer must not read as that
    answer. The fault is in the work tree only: the change stays the same."""
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "later.py").write_text("", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "the change")

    assert _scope_of(checkout, base) == ("code", "false")

    script = checkout / SYSTEMD_RUN
    if fault == "absent":
        script.unlink()
    elif fault == "not executable":
        script.chmod(0o644)
    else:
        script.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")

    assert _scope_of(checkout, base) == ("code", "true")


def test_every_checkout_takes_full_history() -> None:
    """The allocator needs every tag, the scope a merge-base, and some
    tests read git state. A shallow clone has to be proved per job first."""
    for name, job in [*JOBS.items(), *RELEASE_JOBS.items()]:
        for step in job["steps"]:
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue

            assert step["with"]["fetch-depth"] == 0, f"{name} checks out a shallow clone"


@pytest.mark.parametrize("path", _yaml_files(), ids=lambda one: one.parent.name + "/" + one.name)
def test_every_action_is_pinned_or_in_the_tree(path: Path) -> None:
    for found in USES.finditer(path.read_text(encoding="utf-8")):
        name, rest = found.group(1), found.group(2)

        # A local action is read from the checkout: it must be there.
        if name.startswith("./"):
            assert (REPO / name / "action.yml").is_file(), f"{path.name} uses {name}"
            continue

        assert PINNED.match(name), f"{path.name} uses {name}, not a commit"
        assert TAG_COMMENT.match(rest), f"{path.name} uses {name} with no `# vX.Y.Z` beside it"


def test_the_plan_step_plans_nothing_off_main(tmp_path: Path) -> None:
    """A manual run on another branch builds and signs nothing, even when
    the dry run plans a binary tag. Only main writes a transparency record."""
    done, output = _run_plan(tmp_path, VMM_PLAN, "0", ref="refs/heads/topic")

    assert done.returncode == 0, done.stdout + done.stderr
    assert output == "components=\n"
    assert "is not main" in done.stdout
