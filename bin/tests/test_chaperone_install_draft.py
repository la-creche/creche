"""`bin/chaperone-install-draft.py`: the draft of chaperone's install file and manifests.

The golden file holds the whole print for the fixture registry of the
package `family`. `integration/proc/test_proc_install_draft.py` gives the
same golden file to the real `chaperone check`.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "bin" / "chaperone-install-draft.py"
REGISTRY: Final = REPO / "family" / "tests" / "fixtures" / "registry"
GOLDEN: Final = REPO / "bin" / "tests" / "install_draft_golden.txt"

LISTEN: Final = "192.0.2.10:8310"
HEADER: Final = "--- "
EXIT_REFUSED: Final = 2

#: One server file that the draft reads. A test changes one line of it.
SERVER_TEXT: Final = """\
name = "sample"
identity = "A fixture."
install.source = "agent-mcp"
run.entrypoint = "sample-mcp"
run.args = ["--stdio"]
run.env.SAMPLE_URL = "http://127.0.0.1:9"
run.env.SAMPLE_TOKEN = "secret:sample_token"
"""


def _run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(one) for one in args)],
        capture_output=True,
        text=True,
        check=False,
    )


def _files(printed: str) -> dict[str, str]:
    """Each file of a print, by its path. A line `--- <path>` starts each file."""
    files: dict[str, str] = {}
    path = ""

    for line in printed.splitlines(keepends=True):
        if line.startswith(HEADER):
            path = line.removeprefix(HEADER).rstrip("\n")
            files[path] = ""
            continue

        files[path] += line

    return files


def _registry(root: Path, text: str = SERVER_TEXT, name: str = "sample") -> Path:
    server = root / "registry" / "mcp" / name
    server.mkdir(parents=True)
    (server / "server.toml").write_text(text, encoding="utf-8")

    return root / "registry"


def _server_dirs() -> list[str]:
    return sorted(path.name for path in (REGISTRY / "mcp").iterdir() if path.is_dir())


def test_the_draft_of_the_fixture_registry_matches_its_golden() -> None:
    done = _run("--approval-listen", LISTEN, REGISTRY)

    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == GOLDEN.read_text(encoding="utf-8")


def test_the_draft_holds_one_manifest_per_server_and_each_file_is_toml() -> None:
    files = _files(GOLDEN.read_text(encoding="utf-8"))
    install = tomllib.loads(files.pop("install.toml"))
    manifests = {path: tomllib.loads(text) for path, text in files.items()}

    assert set(manifests) == {f"plugins/effect/added/{one}/plugin.toml" for one in _server_dirs()}
    assert install["slots"] == {"approval_channel": "ha-approval"}
    assert install["plugins"]["mcp-http"] == {"guard": "host-firewall"}
    assert install["plugins"]["ha-approval"]["args"][-2:] == ["--listen", LISTEN]
    assert "ha-call" not in install["plugins"]
    for name in _server_dirs():
        assert install["plugins"][name]["user"] == f"mcp-{name}"


def test_the_out_dir_holds_the_printed_files(tmp_path: Path) -> None:
    out = tmp_path / "draft"
    done = _run("--approval-listen", LISTEN, "--out-dir", out, REGISTRY)

    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    written = {
        str(path.relative_to(out)): path.read_text(encoding="utf-8")
        for path in out.rglob("*")
        if path.is_file()
    }
    assert written == _files(GOLDEN.read_text(encoding="utf-8"))


def test_an_out_dir_that_exists_is_refused(tmp_path: Path) -> None:
    done = _run("--approval-listen", LISTEN, "--out-dir", tmp_path, REGISTRY)

    assert done.returncode == EXIT_REFUSED
    assert "exists" in done.stderr
    assert list(tmp_path.iterdir()) == []


def test_no_approval_listen_is_refused() -> None:
    done = _run(REGISTRY)

    assert done.returncode == EXIT_REFUSED
    assert "--approval-listen" in done.stderr


@pytest.mark.parametrize("listen", ["0.0.0.0:8310", "[::]:8310", "192.0.2.10", "a b:1"])
def test_an_approval_listen_that_is_no_address_is_refused(listen: str) -> None:
    done = _run("--approval-listen", listen, REGISTRY)

    assert done.returncode == EXIT_REFUSED
    assert done.stdout == ""


def test_the_roots_come_from_the_flags(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    flags = ("--state-root", "/state", "--mcp-root", "/mcp")
    files = _files(_run("--approval-listen", LISTEN, *flags, registry).stdout)
    install: dict[str, Any] = tomllib.loads(files["install.toml"])
    manifest = tomllib.loads(files["plugins/effect/added/sample/plugin.toml"])

    assert install["plugins"]["attendance"]["args"] == ["--socket", "/state/sock/sessiond.sock"]
    assert manifest["command"] == [
        "/usr/bin/env",
        "SAMPLE_URL=http://127.0.0.1:9",
        "/mcp/sample/bin/sample-mcp",
        "--stdio",
    ]


def test_a_secret_reference_is_a_secret_and_never_an_env_word(tmp_path: Path) -> None:
    files = _files(_run("--approval-listen", LISTEN, _registry(tmp_path)).stdout)
    install = tomllib.loads(files["install.toml"])
    manifest = tomllib.loads(files["plugins/effect/added/sample/plugin.toml"])

    assert install["plugins"]["sample"] == {
        "user": "mcp-sample",
        "secret_delivery": "env",
        "secrets": {"SAMPLE_TOKEN": "sample_token"},
    }
    assert manifest["secrets"] == ["SAMPLE_TOKEN"]
    assert not any("sample_token" in word for word in manifest["command"])


def test_a_state_dir_names_its_variable_last(tmp_path: Path) -> None:
    text = SERVER_TEXT + (
        'run.env.STATE = "/tmp/shared"\nrun.state_dir = true\nrun.state_dir_env = "STATE"\n'
    )
    files = _files(_run("--approval-listen", LISTEN, _registry(tmp_path, text)).stdout)
    manifest = tomllib.loads(files["plugins/effect/added/sample/plugin.toml"])

    assert "STATE=/var/lib/agent-mcp/sample" in manifest["command"]
    assert "STATE=/tmp/shared" not in manifest["command"]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (SERVER_TEXT + 'run.cwd = "/"\n', "run.cwd"),
        (SERVER_TEXT.replace('"sample-mcp"', '"/bin/sh"'), "run.entrypoint"),
        (SERVER_TEXT.replace('name = "sample"', 'name = "other"'), "name"),
        (SERVER_TEXT.replace("secret:sample_token", "secret:Bad-Name"), "SAMPLE_TOKEN"),
        (SERVER_TEXT.replace("http://127.0.0.1:9", "a\\nb"), "SAMPLE_URL"),
        (SERVER_TEXT + "run.state_dir = true\n", "run.state_dir_env"),
        ("name = ", "TOML"),
    ],
    ids=[
        "unknown-run-key",
        "path-entrypoint",
        "other-name",
        "bad-secret",
        "newline",
        "no-var",
        "toml",
    ],
)
def test_a_server_file_the_draft_cannot_read_is_refused(
    tmp_path: Path, text: str, reason: str
) -> None:
    done = _run("--approval-listen", LISTEN, _registry(tmp_path, text))

    assert done.returncode == EXIT_REFUSED
    assert done.stdout == ""
    assert "mcp/sample" in done.stderr
    assert reason in done.stderr


def test_a_yaml_server_file_is_refused(tmp_path: Path) -> None:
    server = tmp_path / "registry" / "mcp" / "sample"
    server.mkdir(parents=True)
    (server / "server.yaml").write_text("name: sample\n", encoding="utf-8")
    done = _run("--approval-listen", LISTEN, tmp_path / "registry")

    assert done.returncode == EXIT_REFUSED
    assert "server.toml" in done.stderr


def test_a_server_named_as_a_plugin_of_chaperone_is_refused(tmp_path: Path) -> None:
    text = SERVER_TEXT.replace('name = "sample"', 'name = "release"')
    done = _run("--approval-listen", LISTEN, _registry(tmp_path, text, "release"))

    assert done.returncode == EXIT_REFUSED
    assert "release" in done.stderr
