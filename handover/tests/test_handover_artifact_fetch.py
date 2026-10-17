"""The operator side fetches the two assets of a binary component.

`corpus.artifacts.fetch_artifacts` runs `curl` as the operator, into the
store below the home of the operator. Root copies the two files later and
checks the signature, so this side fetches and checks no signature.

The catalog holds no binary component, so a test that needs one swaps one
row for a binary copy of itself (`handover_bin_fixtures.py`). Each test but
one gives the fetch a fake `curl`, as `refresh` takes a fake `git`
(`test_handover_r7j_freshness.py`). The last test starts the real
`/usr/bin/curl` against a loopback https server.
"""

from __future__ import annotations

import errno
import math
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import handover.artifacts as pure_half
import pytest
from handover.allocate import TAG_FORMAT, TAG_RE
from handover.artifacts import CODE_HOST, HTTPS, asset_names
from handover.catalog import (
    ARCHIVE_MAX_BYTES,
    BUNDLE_MAX_BYTES,
    CATALOG,
    CATALOG_BY_NAME,
    Action,
    Kind,
    Repo,
)
from handover.corpus import CappedRunFn, CorpusReport, run_capped
from handover.corpus import artifacts as fetching
from handover.corpus.artifacts import (
    CHILD_MAX_S,
    DIR_MODE,
    FILE_MODE,
    LOCKED,
    NO_TIME,
    NOT_LOCKED,
    ROOT_REFUSAL,
    ArtifactReport,
    artifact_store,
    fetch_artifacts,
    ships_archive,
)
from handover.executor.live_state import write_stamp
from handover.executor.mcpbuild import CURL_FLAGS, MAX_ASSET_BYTES
from handover.executor.request import parse_request
from handover.executor.source import CURL, CURL_PROTOCOL_FLAGS
from handover.follow import MARKER_DIR
from handover.resolve import ResolvedComponent
from handover.site import SITE_FILE_ENV
from handover_bin_fixtures import BINARY_NAME, binary_manifest_text, use_binary_catalog
from handover_fixtures import manifest_text, write_manifest

from handover import cli
from handover import corpus as corpus_module

OWNER = "example-owner"
LIVE_VERSION = "0.1.0"
VERSION = "0.1.1"
TAG = f"{BINARY_NAME}-v{VERSION}"
NAMES = asset_names(BINARY_NAME)
ARCHIVE_BYTES = b"an archive of compiled programs\n"
BUNDLE_BYTES = b'{"a": "bundle"}\n'

#: Seconds before a deadline far in the future: each child gets `CHILD_MAX_S`.
LATER = 1_000_000.0


def _url(asset: str) -> str:
    """The download URL, built here from the two constants of the pure half."""
    repo = str(Repo.AGENT_CONTROL)

    return f"{HTTPS}{CODE_HOST}/{OWNER}/{repo}/releases/download/{TAG}/{asset}"


@dataclass
class _Curl:
    """A fake `curl`: it writes the bytes of each URL it serves to the name
    after `--output`, and answers 22 for each other URL, as `curl -f` does
    for an answer of 404."""

    serves: dict[str, bytes] = field(default_factory=dict[str, bytes])
    calls: list[tuple[list[str], float, int]] = field(
        default_factory=list[tuple[list[str], float, int]]
    )
    #: Whether the name after `--output` existed when the child started.
    found_output: list[bool] = field(default_factory=list[bool])
    code: int = 0

    def __call__(self, argv: list[str], timeout_s: float, max_file_bytes: int) -> int:
        self.calls.append((list(argv), timeout_s, max_file_bytes))
        output = Path(argv[argv.index("--output") + 1])
        self.found_output.append(os.path.lexists(output))
        body = self.serves.get(argv[-1])
        if body is None:
            return 22

        # curl makes the file with the mode of the umask.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)

        return self.code


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A site file whose operator home is under `tmp_path`, and a binary
    `noticeboard`."""
    made = tmp_path / "home"
    made.mkdir()
    site = tmp_path / "site.env"
    site.write_text(
        f"AGENT_GITHUB_OWNER={OWNER}\nAGENT_OPERATOR_USER=operator\n"
        f"AGENT_OPERATOR_HOME={made}\nAGENT_LAN_ADDRESS=192.0.2.10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(SITE_FILE_ENV, str(site))
    use_binary_catalog(monkeypatch)

    return made


@pytest.fixture
def store(home: Path) -> Path:
    return artifact_store()


@pytest.fixture
def curl() -> _Curl:
    return _Curl(serves={_url(NAMES.archive): ARCHIVE_BYTES, _url(NAMES.bundle): BUNDLE_BYTES})


def _fetch(
    store: Path, curl: _Curl, left: float = LATER, tags: dict[str, str] | None = None
) -> ArtifactReport:
    """One fetch with `left` seconds before its deadline."""
    wanted = {BINARY_NAME: TAG} if tags is None else tags

    return fetch_artifacts(wanted, store, fetching.monotonic() + left, run=curl, euid=1000)


def _tag_dir(store: Path) -> Path:
    return store / str(Repo.AGENT_CONTROL) / TAG


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# -- the path of the store ---------------------------------------------------


def test_the_store_is_below_the_home_of_the_site_file(home: Path) -> None:
    """Root and the operator compute one path, from the site file."""
    assert artifact_store() == home / ".local" / "state" / "creche" / "artifacts"


def test_the_store_and_the_marker_share_one_directory_name(home: Path) -> None:
    assert artifact_store().parent == home / MARKER_DIR


# -- one source for curl -----------------------------------------------------


def test_the_mcp_fetch_keeps_its_words() -> None:
    """The protocol flags moved to `executor/source.py`, and the words of the
    MCP fetch are the words of before."""
    assert CURL == "/usr/bin/curl"
    assert CURL_PROTOCOL_FLAGS == ("--proto", "=https", "--tlsv1.2")
    assert (
        "-fsSL",
        "--proto",
        "=https",
        "--tlsv1.2",
        "--max-time",
        "600",
        "--max-filesize",
        str(MAX_ASSET_BYTES),
    ) == CURL_FLAGS


# -- the fetch ---------------------------------------------------------------


def test_both_files_land_in_the_store(store: Path, curl: _Curl) -> None:
    report = _fetch(store, curl)

    assert report.ready == frozenset({BINARY_NAME})
    assert (_tag_dir(store) / NAMES.archive).read_bytes() == ARCHIVE_BYTES
    assert (_tag_dir(store) / NAMES.bundle).read_bytes() == BUNDLE_BYTES
    assert sorted(path.name for path in _tag_dir(store).iterdir()) == sorted(
        (NAMES.archive, NAMES.bundle)
    )
    assert report.lines == (
        f"fetched {NAMES.archive} of {TAG}",
        f"fetched {NAMES.bundle} of {TAG}",
    )


def test_the_exact_words_of_the_curl_call(store: Path, curl: _Curl) -> None:
    _fetch(store, curl)

    temp = _tag_dir(store) / f".{NAMES.archive}.part"
    assert curl.calls[0] == (
        [
            CURL,
            "--disable",
            "-fsSL",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--max-time",
            str(CHILD_MAX_S),
            "--max-filesize",
            str(ARCHIVE_MAX_BYTES),
            "--output",
            str(temp),
            _url(NAMES.archive),
        ],
        float(CHILD_MAX_S),
        ARCHIVE_MAX_BYTES,
    )
    assert curl.calls[1][1:] == (float(CHILD_MAX_S), BUNDLE_MAX_BYTES)
    assert curl.calls[1][0][9] == str(BUNDLE_MAX_BYTES)


def test_the_time_limit_of_a_child_is_the_time_that_is_left(store: Path, curl: _Curl) -> None:
    _fetch(store, curl, left=42.7)

    argv, timeout_s, _ = curl.calls[0]
    assert argv[argv.index("--max-time") + 1] == "42"
    assert timeout_s == 42.0


@pytest.mark.parametrize(("deadline", "calls"), [(math.inf, 2), (math.nan, 0)], ids=["inf", "nan"])
def test_a_deadline_that_is_no_finite_number_never_raises(
    store: Path, curl: _Curl, deadline: float, calls: int
) -> None:
    report = fetch_artifacts({BINARY_NAME: TAG}, store, deadline, run=curl, euid=1000)

    assert len(curl.calls) == calls
    assert [call[1] for call in curl.calls] == [float(CHILD_MAX_S)] * calls
    assert report.lines[-1] == (NO_TIME if calls == 0 else f"fetched {NAMES.bundle} of {TAG}")


def test_no_time_left_starts_no_child(store: Path, curl: _Curl) -> None:
    report = _fetch(store, curl, left=0.5)

    assert curl.calls == []
    assert report.ready == frozenset()
    assert report.lines == (NO_TIME,)


def test_a_file_the_server_does_not_have_leaves_the_component_unready(
    store: Path, curl: _Curl
) -> None:
    """A Release gets its assets some seconds after its tag, so this is the
    normal first answer. No final name exists, and no temporary file stays."""
    del curl.serves[_url(NAMES.bundle)]

    report = _fetch(store, curl)

    assert report.ready == frozenset()
    assert not (_tag_dir(store) / NAMES.bundle).exists()
    assert sorted(path.name for path in _tag_dir(store).iterdir()) == [NAMES.archive]
    assert report.lines[-1] == f"{NAMES.bundle} of {TAG}: the server answered with an error"


def test_a_file_over_its_limit_gets_no_final_name(
    store: Path, curl: _Curl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child got the limit twice, as a flag and as a limit of the
    process. A file past it, from a child that kept neither, is refused
    here too."""
    monkeypatch.setattr(pure_half, "BUNDLE_MAX_BYTES", len(BUNDLE_BYTES) - 1)

    report = _fetch(store, curl)

    assert report.ready == frozenset()
    assert curl.calls[1][2] == len(BUNDLE_BYTES) - 1
    assert not (_tag_dir(store) / NAMES.bundle).exists()
    assert not (_tag_dir(store) / f".{NAMES.bundle}.part").exists()
    assert report.lines[-1] == (
        f"{NAMES.bundle} of {TAG}: the fetched file is not a regular file inside its limit"
    )


def test_an_empty_file_gets_no_final_name(store: Path, curl: _Curl) -> None:
    """No asset is an empty file, so an empty answer is fetched again later."""
    curl.serves[_url(NAMES.bundle)] = b""

    report = _fetch(store, curl)

    assert report.ready == frozenset()
    assert sorted(path.name for path in _tag_dir(store).iterdir()) == [NAMES.archive]
    assert report.lines[-1] == (
        f"{NAMES.bundle} of {TAG}: the fetched file is not a regular file inside its limit"
    )


@pytest.mark.parametrize("held", [BUNDLE_BYTES + b"x", b""], ids=["too-large", "empty"])
def test_a_held_file_outside_its_limit_is_fetched_again(
    store: Path, curl: _Curl, monkeypatch: pytest.MonkeyPatch, held: bytes
) -> None:
    monkeypatch.setattr(pure_half, "BUNDLE_MAX_BYTES", len(BUNDLE_BYTES))
    _fetch(store, curl)
    (_tag_dir(store) / NAMES.bundle).write_bytes(held)
    assert _mode(_tag_dir(store) / NAMES.bundle) == FILE_MODE

    report = _fetch(store, curl)

    assert report == ArtifactReport(frozenset({BINARY_NAME}), (f"fetched {NAMES.bundle} of {TAG}",))
    assert (_tag_dir(store) / NAMES.bundle).read_bytes() == BUNDLE_BYTES


def test_with_no_time_left_two_members_give_one_line(
    store: Path, curl: _Curl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch stops at the first member that has no time left."""
    other = "caregiver"
    monkeypatch.setitem(CATALOG_BY_NAME, other, replace(CATALOG_BY_NAME[other], kind=Kind.BINARY))
    tags = {other: f"{other}-v{VERSION}", BINARY_NAME: TAG}

    report = _fetch(store, curl, left=0.5, tags=tags)

    assert curl.calls == []
    assert report == ArtifactReport(frozenset(), (NO_TIME,))
    assert not _tag_dir(store).exists()


def test_a_name_that_is_not_in_the_catalog_is_no_line_and_no_child(
    store: Path, curl: _Curl
) -> None:
    report = _fetch(store, curl, tags={"unknown": "unknown-v1.0.0"})

    assert curl.calls == []
    assert report == ArtifactReport(frozenset(), ())


def test_a_tag_of_the_resolver_is_a_tag_of_the_allocator() -> None:
    """The follower, a request and root build a tag with
    `resolve.TAG_FORMAT`. The fetch reads it with the pattern of the
    allocator, and the prune builds one with `allocate.TAG_FORMAT`. Both
    formats give one tag, so no binary member reads as a tag of another
    shape."""
    for row in CATALOG:
        made = ResolvedComponent(row.name, Action.DEPLOY, None, VERSION).tag()
        matched = TAG_RE.fullmatch(made or "")

        assert made == TAG_FORMAT.format(component=row.name, version=VERSION)
        assert matched is not None
        assert matched.group(1) == row.name


def test_an_exit_status_is_the_one_value_of_the_child_in_a_line(store: Path, curl: _Curl) -> None:
    curl.code = 63

    report = _fetch(store, curl)

    assert report.ready == frozenset()
    assert report.lines == (f"{NAMES.archive} of {TAG}: the fetch exited 63",)
    assert len(curl.calls) == 1
    assert not (_tag_dir(store) / NAMES.archive).exists()


@pytest.mark.parametrize(
    ("fault", "line"),
    [
        (FileNotFoundError(2, "no curl"), "the fetch could not start"),
        (TimeoutError("late"), "the fetch passed its time limit"),
    ],
)
def test_a_child_that_fails_is_a_line_and_never_raises(
    store: Path, fault: OSError, line: str
) -> None:
    def failing(_argv: list[str], _timeout_s: float, _max_bytes: int) -> int:
        raise fault

    deadline = fetching.monotonic() + LATER
    report = fetch_artifacts({BINARY_NAME: TAG}, store, deadline, run=failing, euid=1000)

    assert report == ArtifactReport(frozenset(), (f"{NAMES.archive} of {TAG}: {line}",))


def test_a_run_as_root_is_one_line_and_no_child(store: Path, curl: _Curl) -> None:
    deadline = fetching.monotonic() + LATER
    report = fetch_artifacts({BINARY_NAME: TAG}, store, deadline, run=curl, euid=0)

    assert report == ArtifactReport(frozenset(), (ROOT_REFUSAL,))
    assert curl.calls == []
    assert not store.exists()


@pytest.mark.parametrize(
    "tag", [f"{BINARY_NAME}-v1.2", f"chaperone-v{VERSION}", f"{TAG}\n", f"{BINARY_NAME}-v0.1.x"]
)
def test_a_tag_that_is_not_a_tag_of_the_component_starts_no_child(
    store: Path, curl: _Curl, tag: str
) -> None:
    report = _fetch(store, curl, tags={BINARY_NAME: tag})

    assert curl.calls == []
    assert report.ready == frozenset()
    assert report.lines == (
        f"{BINARY_NAME}: the tag is not a tag of this component, so nothing is fetched",
    )


def test_a_component_of_another_kind_starts_no_child(store: Path, curl: _Curl) -> None:
    """`chaperone` is a venv in the catalog: it ships no archive."""
    report = _fetch(store, curl, tags={"chaperone": f"chaperone-v{VERSION}"})

    assert not ships_archive("chaperone")
    assert curl.calls == []
    assert report == ArtifactReport(frozenset(), ())


def test_only_a_binary_row_ships_an_archive(home: Path) -> None:
    assert ships_archive(BINARY_NAME)
    assert [name for name in ("chaperone", "playpen", "infra") if ships_archive(name)] == []


def test_a_held_component_starts_no_child(store: Path, curl: _Curl) -> None:
    _fetch(store, curl)
    first = len(curl.calls)

    report = _fetch(store, curl)

    assert len(curl.calls) == first
    assert report == ArtifactReport(frozenset({BINARY_NAME}), ())


def test_the_modes_are_exact_whatever_the_umask(store: Path, curl: _Curl) -> None:
    """Root checks the modes, so this side writes them exactly."""
    old = os.umask(0)
    try:
        _fetch(store, curl)
    finally:
        os.umask(old)

    for path in (store, store / str(Repo.AGENT_CONTROL), _tag_dir(store)):
        assert _mode(path) == DIR_MODE, path

    for name in (NAMES.archive, NAMES.bundle):
        assert _mode(_tag_dir(store) / name) == FILE_MODE, name


def test_absent_parents_of_the_store_get_the_mode_whatever_the_umask(
    home: Path, store: Path, curl: _Curl
) -> None:
    old = os.umask(0)
    try:
        _fetch(store, curl)
    finally:
        os.umask(old)

    for path in (home / ".local", home / ".local" / "state", store.parent):
        assert _mode(path) == DIR_MODE, path


def test_a_directory_of_another_mode_gets_the_mode_and_no_child(store: Path, curl: _Curl) -> None:
    _fetch(store, curl)
    _tag_dir(store).chmod(0o755)
    calls = len(curl.calls)

    report = _fetch(store, curl)

    assert report.ready == frozenset({BINARY_NAME})
    assert len(curl.calls) == calls
    assert _mode(_tag_dir(store)) == DIR_MODE


def test_a_file_of_another_mode_is_fetched_again(store: Path, curl: _Curl) -> None:
    _fetch(store, curl)
    (_tag_dir(store) / NAMES.bundle).chmod(0o644)

    report = _fetch(store, curl)

    assert report.ready == frozenset({BINARY_NAME})
    assert report.lines == (f"fetched {NAMES.bundle} of {TAG}",)
    assert _mode(_tag_dir(store) / NAMES.bundle) == FILE_MODE


def test_a_temporary_file_of_an_older_run_goes_first(store: Path, curl: _Curl) -> None:
    """A partial file is never resumed: the child starts with no file."""
    _tag_dir(store).mkdir(parents=True)
    (_tag_dir(store) / f".{NAMES.archive}.part").write_bytes(b"half of an arch")

    report = _fetch(store, curl)

    assert curl.found_output == [False, False]
    assert (_tag_dir(store) / NAMES.archive).read_bytes() == ARCHIVE_BYTES
    assert report.ready == frozenset({BINARY_NAME})


def test_a_link_in_place_of_a_store_directory_is_a_line_and_no_child(
    store: Path, curl: _Curl, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=DIR_MODE)
    _tag_dir(store).parent.mkdir(parents=True)
    _tag_dir(store).symlink_to(elsewhere)

    report = _fetch(store, curl)

    assert curl.calls == []
    assert report == ArtifactReport(frozenset(), (f"{TAG}: the store directory could not be made",))
    assert list(elsewhere.iterdir()) == []


def test_a_second_fetch_of_one_tag_starts_no_child(store: Path, curl: _Curl) -> None:
    """A run of the follower and a request can fetch one tag at one time.
    The second fetch starts while the child of the first one writes. It gets
    one line, and the file of the first child keeps its name."""
    second = _Curl(serves=dict(curl.serves))
    inner: list[ArtifactReport] = []

    def first(argv: list[str], timeout_s: float, max_file_bytes: int) -> int:
        if not inner:
            inner.append(_fetch(store, second))

        return curl(argv, timeout_s, max_file_bytes)

    deadline = fetching.monotonic() + LATER
    report = fetch_artifacts({BINARY_NAME: TAG}, store, deadline, run=first, euid=1000)

    assert inner == [ArtifactReport(frozenset(), (LOCKED.format(tag=TAG),))]
    assert second.calls == []
    assert report.ready == frozenset({BINARY_NAME})
    assert (_tag_dir(store) / NAMES.archive).read_bytes() == ARCHIVE_BYTES
    assert (_tag_dir(store) / NAMES.bundle).read_bytes() == BUNDLE_BYTES


def test_a_lock_that_fails_is_a_line_and_no_child(
    store: Path, curl: _Curl, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_lock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "no lock")

    monkeypatch.setattr(fetching.fcntl, "flock", no_lock)

    report = _fetch(store, curl)

    assert curl.calls == []
    assert report == ArtifactReport(frozenset(), (NOT_LOCKED.format(tag=TAG),))


def test_a_site_file_with_no_owner_starts_no_child(
    store: Path, curl: _Curl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "lacking.env"
    site.write_text(f"AGENT_OPERATOR_HOME={tmp_path / 'home'}\n", encoding="utf-8")
    monkeypatch.setenv(SITE_FILE_ENV, str(site))

    report = _fetch(store, curl)

    assert curl.calls == []
    assert report.lines == (
        f"{BINARY_NAME}: the site file gives no download URL, so nothing is fetched",
    )


# -- the child -----------------------------------------------------------------


def test_the_child_writes_no_file_past_its_limit(tmp_path: Path) -> None:
    """The kernel counts the bytes as the child writes them, so the limit
    holds with no length from a server."""
    target = tmp_path / "big"
    script = f"with open({str(target)!r}, 'wb') as out: out.write(b'x' * 4096)"

    code = run_capped([sys.executable, "-I", "-c", script], 60.0, 1024)

    assert code != 0
    assert target.stat().st_size <= 1024


def test_the_child_gets_path_and_lang_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """curl needs no credential and no setting of the caller."""
    envs: list[dict[str, str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        envs.append(dict(cast("dict[str, str]", kwargs["env"])))

        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("HOME", "/nonexistent")
    monkeypatch.setattr(corpus_module.subprocess, "run", run)

    assert run_capped([CURL, "--version"], 5.0, 1024) == 0
    assert [sorted(env) for env in envs] == [["LANG", "PATH"]]
    assert envs[0]["LANG"] == "C.UTF-8"


def test_a_child_past_its_time_limit_raises_a_timeout(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError):
        run_capped([sys.executable, "-I", "-c", "import time; time.sleep(30)"], 0.5, 1024)


# -- the real curl -------------------------------------------------------------

#: A loopback https server: one file of the directory for each name in the
#: last segment of the path, and 404 for each other path. A name in the last
#: argument gets no length in the answer.
_SERVER = """\
import http.server, pathlib, ssl, sys

root = pathlib.Path(sys.argv[1])
unsized = set(sys.argv[4].split(","))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        path = root / name
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        if name not in unsized:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(sys.argv[2], sys.argv[3])
server.socket = context.wrap_socket(server.socket, server_side=True)
print(server.server_address[1], flush=True)
server.serve_forever()
"""


@dataclass(frozen=True)
class _Served:
    root: Path
    port: int
    cert: Path


def _certificate(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway self-signed certificate for the name of the code host."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not on this machine, so no TLS listener can be built here")

    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            f"/CN={CODE_HOST}",
            "-addext",
            f"subjectAltName=DNS:{CODE_HOST}",
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )

    return cert, key


@pytest.fixture
def served(tmp_path: Path) -> Iterator[_Served]:
    """The server in a child process. A thread in this process would make
    the limit of the child unsafe to set (`corpus.run_capped`)."""
    if not os.access(CURL, os.X_OK):
        pytest.skip("curl is not on this machine")

    cert, key = _certificate(tmp_path)
    root = tmp_path / "release"
    root.mkdir()
    (root / NAMES.archive).write_bytes(ARCHIVE_BYTES * 64)
    (root / NAMES.bundle).write_bytes(BUNDLE_BYTES)
    script = tmp_path / "server.py"
    script.write_text(_SERVER, encoding="utf-8")
    argv = [sys.executable, "-I", str(script), str(root), str(cert), str(key), NAMES.bundle]
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    try:
        assert child.stdout is not None
        port = int(child.stdout.readline())
        yield _Served(root, port, cert)
    finally:
        child.terminate()
        child.wait(timeout=30)
        if child.stdout is not None:
            child.stdout.close()


def _real_curl(served: _Served) -> CappedRunFn:
    """The real `run_capped` and the real curl. Two words after `--disable`
    send the connection to the loopback server and trust its certificate."""

    def run(argv: list[str], timeout_s: float, max_file_bytes: int) -> int:
        local = ["--cacert", str(served.cert), "--connect-to", f"::127.0.0.1:{served.port}"]

        return run_capped([*argv[:2], *local, *argv[2:]], timeout_s, max_file_bytes)

    return run


def test_the_real_curl_fetches_both_files_from_a_loopback_server(
    store: Path, served: _Served
) -> None:
    report = fetch_artifacts(
        {BINARY_NAME: TAG}, store, fetching.monotonic() + 120, run=_real_curl(served), euid=1000
    )

    assert report.ready == frozenset({BINARY_NAME}), report.lines
    assert (_tag_dir(store) / NAMES.archive).read_bytes() == ARCHIVE_BYTES * 64
    assert (_tag_dir(store) / NAMES.bundle).read_bytes() == BUNDLE_BYTES
    assert _mode(_tag_dir(store) / NAMES.archive) == FILE_MODE


def test_the_real_curl_stops_at_the_limit_with_no_length_and_with_one(
    store: Path, served: _Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server sends the bundle with no length, so only the limit of the
    process stops it. It sends the archive with a length, so the flag of
    curl stops it before a byte lands."""
    monkeypatch.setattr(pure_half, "ARCHIVE_MAX_BYTES", len(ARCHIVE_BYTES))
    monkeypatch.setattr(pure_half, "BUNDLE_MAX_BYTES", len(BUNDLE_BYTES) - 4)
    run = _real_curl(served)
    deadline = fetching.monotonic() + 120

    archive = fetch_artifacts({BINARY_NAME: TAG}, store, deadline, run=run, euid=1000)
    (served.root / NAMES.archive).write_bytes(ARCHIVE_BYTES)
    bundle = fetch_artifacts({BINARY_NAME: TAG}, store, deadline, run=run, euid=1000)

    assert archive.ready == bundle.ready == frozenset()
    assert archive.lines == (f"{NAMES.archive} of {TAG}: the fetch exited 63",)
    assert bundle.lines[0] == f"fetched {NAMES.archive} of {TAG}"
    assert bundle.lines[1].startswith(f"{NAMES.bundle} of {TAG}: the fetch exited ")
    assert sorted(path.name for path in _tag_dir(store).iterdir()) == [NAMES.archive]


# -- handover request ------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@dataclass(frozen=True)
class _Desk:
    """Where `handover request` reads and files, under `tmp_path`."""

    options: list[str]
    spool: Path
    control: Path


def _desk(tmp_path: Path, live: tuple[str, ...]) -> _Desk:
    """Every manifest, with the binary one for `noticeboard`, three clones,
    and each `live` component installed and tagged at `LIVE_VERSION`."""
    roots = tmp_path / "repo"
    installs = tmp_path / "components"
    for row in CATALOG:
        text = binary_manifest_text(installs) if row.name == BINARY_NAME else None
        write_manifest(roots, row.name, text or manifest_text(row.name))

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    corpus.chmod(0o755)
    for repo in Repo:
        clone = corpus / str(repo)
        clone.mkdir()
        clone.chmod(0o755)
        (clone / "README.md").write_text(f"{repo}\n", encoding="utf-8")
        _git(clone, "init", "-q", "-b", "main")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "first")

    for name in live:
        _git(corpus / str(CATALOG_BY_NAME[name].repo), "tag", f"{name}-v{LIVE_VERSION}")
        tree = installs / name
        tree.mkdir(parents=True)
        write_stamp(tree, LIVE_VERSION)

    spool = tmp_path / "spool"
    spool.mkdir()
    options = ["--root", str(roots), "--partial", "--corpus", str(corpus)]
    options += ["--install-root", str(installs), "--spool", str(spool)]

    return _Desk(options, spool, corpus / str(Repo.AGENT_CONTROL))


@dataclass
class _Polls:
    """`cli.refresh` for a request: poll N lands the tags of `lands[N]` and
    publishes the assets of `publishes[N]`."""

    desk: _Desk
    curl: _Curl
    lands: dict[int, str] = field(default_factory=dict[int, str])
    publishes: dict[int, Callable[[], None]] = field(default_factory=dict[int, Callable[[], None]])
    polls: int = 0

    def __call__(self, repos: tuple[str, ...], *_args: object, **_kwargs: object) -> CorpusReport:
        self.polls += 1
        tag = self.lands.get(self.polls)
        if tag is not None:
            _git(self.desk.control, "tag", tag)

        publish = self.publishes.get(self.polls)
        if publish is not None:
            publish()

        return CorpusReport(True, [f"fetched {name}" for name in repos])


@pytest.fixture
def desk(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> _Desk:
    del home
    made = _desk(tmp_path, (BINARY_NAME, "chaperone"))
    _git(made.control, "tag", TAG)

    return made


def _filed(desk: _Desk) -> list[dict[str, str]]:
    return [
        parse_request(path.read_bytes(), path.stem).wanted()
        for path in sorted(desk.spool.glob("*.json"))
    ]


def _empty_curl() -> _Curl:
    return _Curl()


def _publish(curl: _Curl) -> None:
    curl.serves.update({_url(NAMES.archive): ARCHIVE_BYTES, _url(NAMES.bundle): BUNDLE_BYTES})


def test_a_request_fetches_both_files_of_a_binary_component_that_deploys(
    desk: _Desk, store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    curl = _empty_curl()
    _publish(curl)
    monkeypatch.setattr(fetching, "run_capped", curl)
    monkeypatch.setattr(cli, "refresh", _Polls(desk, curl))
    # A clock that stands, so the time limit of the child is exact.
    monkeypatch.setattr(fetching, "monotonic", lambda: 0.0)
    monkeypatch.setattr(cli, "monotonic", lambda: 0.0)

    code = cli.main(["request", BINARY_NAME, *desk.options])

    err = capsys.readouterr().err
    assert code == cli.EXIT_OK, err
    assert _filed(desk) == [{BINARY_NAME: "latest"}]
    assert (_tag_dir(store) / NAMES.archive).read_bytes() == ARCHIVE_BYTES
    assert f"note: fetched {NAMES.bundle} of {TAG}\n" in err
    argv = curl.calls[0][0]
    assert argv[argv.index("--max-time") + 1] == str(min(cli.REQUEST_FETCH_S, CHILD_MAX_S))


def test_a_request_fetches_no_binary_component_that_does_not_deploy(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`noticeboard` is live at its newest tag, so it is `unchanged` in the
    resolution of a request for `chaperone`."""
    del home
    desk = _desk(tmp_path, (BINARY_NAME, "chaperone"))
    _git(desk.control, "tag", f"chaperone-v{VERSION}")
    curl = _empty_curl()
    _publish(curl)
    monkeypatch.setattr(fetching, "run_capped", curl)
    monkeypatch.setattr(cli, "refresh", _Polls(desk, curl))

    assert cli.main(["request", "chaperone", *desk.options]) == cli.EXIT_OK

    assert curl.calls == []
    assert _filed(desk) == [{"chaperone": "latest"}]


def test_a_request_whose_fetch_fails_still_files_and_says_so(
    desk: _Desk, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Root answers `no artifact in the store` before the tap, so a request
    that refused to file here would only cost the operator a command."""
    curl = _empty_curl()
    monkeypatch.setattr(fetching, "run_capped", curl)
    monkeypatch.setattr(cli, "refresh", _Polls(desk, curl))

    code = cli.main(["request", BINARY_NAME, *desk.options])

    err = capsys.readouterr().err
    assert code == cli.EXIT_OK, err
    assert _filed(desk) == [{BINARY_NAME: "latest"}]
    assert f"note: {NAMES.archive} of {TAG}: the server answered with an error\n" in err


def test_each_poll_of_a_wait_fetches_again(desk: _Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    """The assets are published on poll 2, and the tag of `chaperone` lands
    on poll 3. Poll 1 asks for the archive, poll 2 for both files, and poll 3
    finds both in the store."""
    curl = _empty_curl()
    polls = _Polls(
        desk, curl, lands={3: f"chaperone-v{VERSION}"}, publishes={2: lambda: _publish(curl)}
    )
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(fetching, "run_capped", curl)
    monkeypatch.setattr(fetching, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli, "refresh", polls)
    monkeypatch.setattr(cli, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli, "sleep", sleep)

    code = cli.main(["request", BINARY_NAME, "chaperone", *desk.options, "--wait"])

    assert code == cli.EXIT_OK
    assert polls.polls == 3
    assert [call[0][-1] for call in curl.calls] == [
        _url(NAMES.archive),
        _url(NAMES.archive),
        _url(NAMES.bundle),
    ]
    assert _filed(desk) == [{BINARY_NAME: "latest", "chaperone": "latest"}]


def test_a_dry_run_with_no_wait_fetches_no_artifact(
    desk: _Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It fetches no corpus either, as it does today."""
    curl = _empty_curl()
    _publish(curl)
    monkeypatch.setattr(fetching, "run_capped", curl)
    monkeypatch.setattr(cli, "refresh", _Polls(desk, curl))

    assert cli.main(["request", BINARY_NAME, *desk.options, "--dry-run"]) == cli.EXIT_OK

    assert curl.calls == []
