"""`playpen/bin/playpen-build` and `playpen/bin/playpen-verify`, with no host.

A `playpen` release is the build script at step 8 and the verify hook after
the switch. Both run here against two stubs on `PATH`: a `docker` that
records its argv and a `curl` that answers as the registry. The registry
stub holds what a `docker push` put there, so the two scripts read one
answer, as they do on a host.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from caregiver.images import SandboxImages
from caregiver.released import read_images

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD = REPO_ROOT / "playpen" / "bin" / "playpen-build"
VERIFY = REPO_ROOT / "playpen" / "bin" / "playpen-verify"
LIBRARY = REPO_ROOT / "playpen" / "bin" / "lib" / "registry.sh"

REGISTRY = "127.0.0.1:5000"
LAN = "192.0.2.10"
BASE = "agent-sandbox"
PYTHON = "agent-sandbox-python"
DIGEST = {BASE: "sha256:" + "a" * 64, PYTHON: "sha256:" + "b" * 64}

#: `docker`: one line of argv per call. A `push` puts the image's digest
#: into the registry stub. `STUB_FAIL` names the one verb that fails.
DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
[[ "$1" == "${STUB_FAIL:-}" ]] && exit 1
if [[ "$1" == push ]]; then
  name="${2#*/}"; name="${name%%:*}"
  grep -E "^$name " "$STUB_DIGESTS" >> "$STUB_HELD"
fi
exit 0
"""

#: `curl`: the registry. It answers a manifest request with the digest it
#: holds for that name, and exits 22 for a name it does not hold, the way
#: `curl -f` answers a 404.
CURL_STUB = """#!/usr/bin/env bash
url="${@: -1}"
name="${url#*/v2/}"; name="${name%%/manifests/*}"
digest="$(grep -E "^$name " "$STUB_HELD" 2>/dev/null | tail -n 1 | cut -d' ' -f2)"
[[ -n "$digest" ]] || exit 22
printf 'HTTP/1.1 200 OK\\r\\nDocker-Content-Digest: %s\\r\\n\\r\\n' "$digest"
"""


@dataclass
class _Host:
    root: Path

    @property
    def staged(self) -> Path:
        return self.root / "playpen.new"

    @property
    def held(self) -> Path:
        return self.root / "held"

    @property
    def site(self) -> Path:
        return self.root / "site.env"

    def env(self, **more: str) -> dict[str, str]:
        return {
            "PATH": f"{self.root / 'stubs'}:/usr/bin:/bin",
            "HOME": str(self.root),
            "STUB_LOG": str(self.root / "docker.log"),
            "STUB_DIGESTS": str(self.root / "digests"),
            "STUB_HELD": str(self.held),
            "AGENT_SITE_FILE": str(self.site),
            "UV_PROJECT_ENVIRONMENT": str(self.staged),
            **more,
        }

    def build(self, **more: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(BUILD)],
            env=self.env(**more),
            capture_output=True,
            text=True,
            check=False,
            cwd=self.root,
        )

    def verify(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.staged / "bin" / "playpen-verify"), *argv],
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
            cwd=self.root,
        )

    def docker_calls(self) -> list[str]:
        log = self.root / "docker.log"

        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def images(self) -> dict[str, str]:
        lines = (self.staged / "images.env").read_text(encoding="utf-8").splitlines()

        return dict(line.split("=", 1) for line in lines)


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, text in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
        (stubs / name).write_text(text, encoding="utf-8")
        (stubs / name).chmod(0o755)

    made = _Host(tmp_path)
    made.site.write_text(
        f"AGENT_OPERATOR_USER=someone\nAGENT_LAN_ADDRESS={LAN}\n", encoding="utf-8"
    )
    lines = "".join(f"{name} {digest}\n" for name, digest in DIGEST.items())
    (tmp_path / "digests").write_text(lines, encoding="utf-8")

    return made


# -- the build ----------------------------------------------------------------


def test_the_build_stages_one_digest_per_flavor(host: _Host) -> None:
    done = host.build()

    assert done.returncode == 0, done.stderr
    assert host.images() == {
        "base": f"{REGISTRY}/{BASE}@{DIGEST[BASE]}",
        "python": f"{REGISTRY}/{PYTHON}@{DIGEST[PYTHON]}",
    }


def test_the_caregiver_reads_the_file_the_build_wrote(host: _Host) -> None:
    """Two programs in two languages hold one file shape. This is the test
    that the writer and the reader agree."""
    host.build()

    assert read_images(host.staged / "images.env") == SandboxImages(
        base=f"{REGISTRY}/{BASE}@{DIGEST[BASE]}",
        python=f"{REGISTRY}/{PYTHON}@{DIGEST[PYTHON]}",
    )


def test_the_build_stages_the_verify_hook(host: _Host) -> None:
    """Contract 06 §4 rule 6: the hook ships in the artifact. The executor
    refuses a staged tree that does not hold `verify.command[0]`."""
    host.build()

    hook = host.staged / "bin" / "playpen-verify"
    assert hook.read_bytes() == VERIFY.read_bytes()
    assert hook.stat().st_mode & 0o111 == 0o111
    assert (host.staged / "bin" / "lib" / "registry.sh").read_bytes() == LIBRARY.read_bytes()


def test_the_build_gives_docker_the_sites_address(host: _Host) -> None:
    """`playpen/build.mjs` refuses to bundle without it, and a sandbox has
    no site file to read it from."""
    host.build()

    builds = [line for line in host.docker_calls() if line.startswith("build ")]
    assert len(builds) == 2
    for line, target in zip(builds, (BASE, PYTHON), strict=True):
        assert f"--build-arg AGENT_LAN_ADDRESS={LAN}" in line
        assert f"--target {target} " in line
        assert f"-f {REPO_ROOT}/playpen/Dockerfile" in line
        assert line.endswith(f" {REPO_ROOT}")


def test_the_build_pushes_what_it_built(host: _Host) -> None:
    host.build()

    calls = host.docker_calls()
    built = [word for line in calls if line.startswith("build ") for word in line.split()]
    pushed = [line.split()[1] for line in calls if line.startswith("push ")]

    assert len(pushed) == 2
    assert all(reference in built for reference in pushed)
    assert pushed[0].startswith(f"{REGISTRY}/{BASE}:")
    assert pushed[1].startswith(f"{REGISTRY}/{PYTHON}:")


def test_no_site_address_stages_nothing(host: _Host) -> None:
    host.site.write_text("AGENT_OPERATOR_USER=someone\n", encoding="utf-8")

    done = host.build()

    assert done.returncode == 1
    assert "AGENT_LAN_ADDRESS" in done.stderr
    assert not host.staged.exists()
    assert host.docker_calls() == []


@pytest.mark.parametrize("value", ["10.0.0.1 --privileged", "a;b", "-x", ""])
def test_an_address_that_is_not_one_word_stages_nothing(host: _Host, value: str) -> None:
    """The value becomes one argv word of a command root runs."""
    host.site.write_text(f"AGENT_LAN_ADDRESS={value}\n", encoding="utf-8")

    done = host.build()

    assert done.returncode == 1
    assert not host.staged.exists()
    assert host.docker_calls() == []


def test_no_staged_tree_named_is_a_refusal(host: _Host) -> None:
    done = host.build(UV_PROJECT_ENVIRONMENT="")

    assert done.returncode == 1
    assert "UV_PROJECT_ENVIRONMENT" in done.stderr
    assert host.docker_calls() == []


@pytest.mark.parametrize("verb", ["build", "push"])
def test_a_failed_docker_step_stages_nothing(host: _Host, verb: str) -> None:
    """Everything builds before anything is staged. The executor then finds
    no tree, and the host is as it was."""
    done = host.build(STUB_FAIL=verb)

    assert done.returncode == 1
    assert "Nothing was staged" in done.stderr
    assert not host.staged.exists()


def test_a_registry_that_names_no_digest_stages_nothing(host: _Host) -> None:
    (host.root / "digests").write_text("", encoding="utf-8")

    done = host.build()

    assert done.returncode == 1
    assert "no digest" in done.stderr
    assert not host.staged.exists()


def test_the_build_reads_nothing_outside_its_own_directory() -> None:
    """Contract 06 §1 rule 9: a tag covers a component's own paths. A
    script that sourced a file of another directory would build something
    no `playpen` tag covers."""
    text = BUILD.read_text(encoding="utf-8")
    sourced = [line.split()[1] for line in text.splitlines() if line.startswith(". ")]

    assert sourced == ['"$ROOT/playpen/bin/lib/registry.sh"']


# -- the hook -----------------------------------------------------------------


def test_the_hook_passes_on_the_tree_the_build_staged(host: _Host) -> None:
    host.build()

    done = host.verify("--json")

    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout)
    assert report["ok"] is True
    assert [row["flavor"] for row in report["checks"]] == ["base", "python"]
    assert all(row["ok"] for row in report["checks"])


def test_the_hook_fails_when_the_registry_lost_an_image(host: _Host) -> None:
    host.build()
    kept = [line for line in host.held.read_text().splitlines() if line.startswith(f"{PYTHON} ")]
    host.held.write_text("\n".join(kept) + "\n", encoding="utf-8")

    done = host.verify("--json")

    assert done.returncode == 1
    report = json.loads(done.stdout)
    assert report["ok"] is False
    assert [row["ok"] for row in report["checks"]] == [False, True]


def test_the_hook_fails_when_the_registry_holds_another_digest(host: _Host) -> None:
    host.build()
    host.held.write_text(f"{BASE} sha256:{'c' * 64}\n{PYTHON} {DIGEST[PYTHON]}\n", encoding="utf-8")

    assert host.verify("--json").returncode == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"python={REGISTRY}/{PYTHON}@{DIGEST[PYTHON]}\n",
        f"base={REGISTRY}/{BASE}:latest\n",
        f"base={REGISTRY}/{BASE}@sha256:short\n",
        f"base={REGISTRY}/{BASE}@{DIGEST[BASE]} extra\n",
    ],
)
def test_the_hook_fails_on_a_tree_that_names_no_base_digest(host: _Host, text: str) -> None:
    host.build()
    (host.staged / "images.env").write_text(text, encoding="utf-8")

    done = host.verify("--json")

    assert done.returncode == 1
    assert json.loads(done.stdout)["ok"] is False


def test_the_hook_fails_on_a_tree_with_no_images_file(host: _Host) -> None:
    host.build()
    (host.staged / "images.env").unlink()

    assert host.verify("--json").returncode == 1


def test_the_hook_writes_nothing(host: _Host) -> None:
    """Contract 06 §4 rule 1."""
    host.build()
    before = sorted(str(path) for path in host.root.rglob("*"))

    host.verify("--json")

    assert sorted(str(path) for path in host.root.rglob("*")) == before
    assert not any(line.startswith(("pull", "run")) for line in host.docker_calls())


def test_the_hook_without_json_prints_lines(host: _Host) -> None:
    host.build()

    done = host.verify()

    assert done.returncode == 0
    assert "playpen-verify: PASS" in done.stdout


def test_an_unknown_argument_is_a_usage_mistake(host: _Host) -> None:
    host.build()

    assert host.verify("--write").returncode == 2


# -- the two files in the index -------------------------------------------------


@pytest.mark.parametrize("script", [BUILD, VERIFY])
def test_both_scripts_are_executable(script: Path) -> None:
    """The build installs the hook with its own mode, and the executor runs
    it as the operator."""
    assert os.stat(script).st_mode & stat.S_IXUSR
