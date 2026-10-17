"""`creche_env.envfile` and the command `creche-env`, beyond the vectors.

The vectors of `envfile.*` hold the grammar and the layers
(`bin/tests/test_envfile_vectors.py` runs the command on each one). The
tests here hold what no vector can: a real file with its owner and its mode,
the password database of this machine, the options of the command and the
package data.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest
from creche_env import envfile
from creche_env.cli import EX_CONFIG, main
from creche_env.envfile import EnvError, Fault, ReadError, Text

PAIR_DEFAULT = "ROOT=/srv/data\nSTATE=${ROOT}/state\nHOME_DIR=${home:ACCOUNT}\nTRUSTED=/opt\n"
PAIR_EXAMPLE = "# The required keys.\n#ACCOUNT=operator\n"
ROOT_KEYS = "TRUSTED\nSTATE\n"


def _write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)

    return path


def _pair(directory: Path, default: str = PAIR_DEFAULT, example: str = PAIR_EXAMPLE) -> Path:
    directory.mkdir(exist_ok=True)
    _write(directory / "default.env", default, 0o644)
    _write(directory / "example.env", example, 0o644)
    _write(directory / "root-keys.txt", ROOT_KEYS, 0o644)

    return directory


def _me() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _run(capsys: pytest.CaptureFixture[str], *argv: str | Path) -> tuple[int, str, str]:
    status = main([str(one) for one in argv])
    printed = capsys.readouterr()

    return status, printed.out, printed.err


# --- the trust check of a real file -------------------------------------------


def test_a_private_file_of_the_reader_is_read(tmp_path: Path) -> None:
    path = _write(tmp_path / "site.env", "A=1\n")

    assert envfile.read_trusted(path) == Text(str(path), b"A=1\n")


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_a_file_that_group_or_other_can_write_is_refused(tmp_path: Path, mode: int) -> None:
    path = _write(tmp_path / "site.env", "A=1\n", mode)

    with pytest.raises(EnvError) as caught:
        envfile.read_trusted(path)

    assert [one.fault for one in caught.value.problems] == [Fault.OTHERS_WRITE]


def test_a_file_of_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path / "site.env", "A=1\n")
    owner = os.stat(path).st_uid
    if owner == envfile.ROOT_UID:
        pytest.skip("the test runs as root, and each account reads a file of root")

    monkeypatch.setattr(os, "getuid", lambda: owner + 1)

    with pytest.raises(EnvError) as caught:
        envfile.read_trusted(path)

    assert [one.fault for one in caught.value.problems] == [Fault.WRONG_OWNER]


def test_root_reads_only_a_file_of_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path / "site.env", "A=1\n")
    if os.stat(path).st_uid == envfile.ROOT_UID:
        pytest.skip("the test runs as root, so each file of the test is a file of root")

    monkeypatch.setattr(os, "getuid", lambda: envfile.ROOT_UID)

    with pytest.raises(EnvError) as caught:
        envfile.read_trusted(path)

    assert [one.fault for one in caught.value.problems] == [Fault.WRONG_OWNER]


def test_a_link_is_judged_by_its_target(tmp_path: Path) -> None:
    good = tmp_path / "good.env"
    _write(good, "A=1\n")
    open_target = _write(tmp_path / "open.env", "A=1\n", 0o666)
    (tmp_path / "to-good.env").symlink_to(good)
    (tmp_path / "to-open.env").symlink_to(open_target)

    assert envfile.read_trusted(tmp_path / "to-good.env").data == b"A=1\n"
    with pytest.raises(EnvError):
        envfile.read_trusted(tmp_path / "to-open.env")


def test_an_absent_file_and_a_directory_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(ReadError, match="ENOENT"):
        envfile.read_trusted(tmp_path / "absent.env")

    with pytest.raises(ReadError, match="no regular file"):
        envfile.read_trusted(tmp_path)


def test_the_read_stops_one_byte_past_the_limit(tmp_path: Path) -> None:
    path = _write(tmp_path / "site.env", "#" * (envfile.MAX_BYTES * 2) + "\n")

    assert len(envfile.read_trusted(path).data) == envfile.MAX_BYTES + 1


# --- the password database and the site file ---------------------------------


def test_the_home_layer_asks_the_password_database(tmp_path: Path) -> None:
    pair = envfile.pair_in(_pair(tmp_path / "pair"))
    env = Text(".env", f"ACCOUNT={_me()}\n".encode())

    settings = envfile.load(pair, env)

    assert settings.values["HOME_DIR"] == pwd.getpwuid(os.getuid()).pw_dir


def test_an_account_that_the_database_lacks_has_no_home() -> None:
    assert envfile.system_home("no-such-account-of-this-machine") is None


def test_the_site_file_is_the_one_that_the_variable_names() -> None:
    assert envfile.site_file({}) == Path("/etc/creche/site.env")
    assert envfile.site_file({envfile.SITE_FILE_ENV: ""}) == envfile.SITE_FILE
    assert envfile.site_file({envfile.SITE_FILE_ENV: "/x/site.env"}) == Path("/x/site.env")


def test_the_package_data_is_beside_the_module() -> None:
    names = {one.name for one in envfile.package_dir().iterdir()}

    assert {"default.env", "example.env", "root-keys.txt"} <= names


# --- the command ---------------------------------------------------------------


@pytest.fixture
def pair(tmp_path: Path) -> Path:
    return _pair(tmp_path / "pair")


@pytest.fixture
def site(tmp_path: Path) -> Path:
    return _write(tmp_path / "site.env", f"ACCOUNT={_me()}\nROOT=/data\n")


def test_get_prints_the_value_after_each_layer(
    capsys: pytest.CaptureFixture[str], pair: Path, site: Path
) -> None:
    assert _run(capsys, "get", "STATE", "--pair", pair, "--env", site) == (0, "/data/state\n", "")


def test_get_prints_an_empty_line_for_a_key_with_no_value(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, site: Path
) -> None:
    pair = _pair(tmp_path / "optional", PAIR_DEFAULT + "OPTIONAL=\n")

    assert _run(capsys, "get", "OPTIONAL", "--pair", pair, "--env", site) == (0, "\n", "")


def test_get_reads_the_site_file_that_the_variable_names(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, pair: Path, site: Path
) -> None:
    monkeypatch.setenv(envfile.SITE_FILE_ENV, str(site))

    assert _run(capsys, "get", "ROOT", "--pair", pair) == (0, "/data\n", "")


def test_get_refuses_a_key_outside_the_catalog(
    capsys: pytest.CaptureFixture[str], pair: Path, site: Path
) -> None:
    status, out, err = _run(capsys, "get", "SURPLUS", "--pair", pair, "--env", site)

    assert (status, out) == (EX_CONFIG, "")
    assert err == f"creche-env: {pair / 'default.env'}: unknown-key SURPLUS\n"


def test_get_names_no_text_that_is_not_a_key(
    capsys: pytest.CaptureFixture[str], pair: Path, site: Path
) -> None:
    status, _, err = _run(capsys, "get", "no key\nline", "--pair", pair, "--env", site)

    assert (status, err) == (EX_CONFIG, f"creche-env: {pair / 'default.env'}: bad-key\n")


def test_get_of_the_package_pair(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    site = _write(
        tmp_path / "site.env",
        f"AGENT_GITHUB_OWNER=example-owner\nAGENT_OPERATOR_USER={_me()}\n"
        "AGENT_LAN_ADDRESS=192.0.2.10\n",
    )

    status, out, _ = _run(capsys, "get", "AGENT_PEP_URL", "--env", site)

    assert (status, out) == (0, "http://192.0.2.10:8300\n")


def test_check_prints_nothing(capsys: pytest.CaptureFixture[str], pair: Path, site: Path) -> None:
    assert _run(capsys, "check", "--pair", pair, "--env", site) == (0, "", "")


def test_check_names_each_absent_required_key_on_a_line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    example = _pair(tmp_path / "two", example=PAIR_EXAMPLE + "#SECOND=x\n")
    empty = _write(tmp_path / "empty.env", "")

    status, out, err = _run(capsys, "check", "--pair", example, "--env", empty)

    assert (status, out) == (EX_CONFIG, "")
    assert err.splitlines() == [
        f"creche-env: {example / 'example.env'}:2: required-unset ACCOUNT",
        f"creche-env: {example / 'example.env'}:3: required-unset SECOND",
    ]


def test_check_of_each_catalog(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, pair: Path, site: Path
) -> None:
    older = _pair(tmp_path / "older", "ACCOUNT_LESS=1\nHOME_DIR=/x\nSTATE=/s\nTRUSTED=/t\n")

    assert _run(capsys, "check", "--catalog", pair, pair, "--site", site) == (0, "", "")

    status, _, err = _run(capsys, "check", "--catalog", pair, "--catalog", older, "--site", site)

    assert status == EX_CONFIG
    assert err == f"creche-env: {site}:2: unknown-key ROOT\n"


@pytest.mark.parametrize(
    "words",
    [
        ("check", "--catalog", "x", "--pair", "z"),
        ("check", "--site", "y", "--env", "z"),
        ("get",),
        ("raw", "x"),
        ("other",),
    ],
)
def test_a_command_line_that_the_command_cannot_read(
    capsys: pytest.CaptureFixture[str], words: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(list(words))

    assert caught.value.code == 2
    assert capsys.readouterr().out == ""


def test_a_file_that_cannot_be_read_is_one_line_and_78(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, pair: Path
) -> None:
    absent = tmp_path / "absent.env"

    assert _run(capsys, "check", "--pair", pair, "--env", absent) == (
        EX_CONFIG,
        "",
        f"creche-env: {absent}: cannot be read: ENOENT\n",
    )


def test_an_absent_pair_cannot_be_read(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    status, _, err = _run(capsys, "check", "--pair", tmp_path / "none", "--env", tmp_path)

    assert status == EX_CONFIG
    assert err == f"creche-env: {tmp_path / 'none' / 'default.env'}: cannot be read: ENOENT\n"


def test_the_env_file_must_pass_the_trust_check(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, pair: Path
) -> None:
    site = _write(tmp_path / "open.env", f"ACCOUNT={_me()}\n", 0o664)

    assert _run(capsys, "check", "--pair", pair, "--env", site) == (
        EX_CONFIG,
        "",
        f"creche-env: {site}: others-write\n",
    )


def test_changed_trusted_names_each_root_key_that_changed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, pair: Path, site: Path
) -> None:
    same = _write(tmp_path / "same.env", f"ROOT=/data\nACCOUNT={_me()}\n")
    moved = _write(tmp_path / "moved.env", f"ACCOUNT={_me()}\nROOT=/other\nTRUSTED=/x\n")

    assert _run(capsys, "changed-trusted", site, same, "--pair", pair) == (0, "", "")
    assert _run(capsys, "changed-trusted", site, moved, "--pair", pair) == (
        0,
        "TRUSTED\nSTATE\n",
        "",
    )


def test_changed_trusted_refuses_a_root_key_outside_the_catalog(
    capsys: pytest.CaptureFixture[str], pair: Path, site: Path
) -> None:
    _write(pair / "root-keys.txt", "TRUSTED\nSURPLUS\n", 0o644)

    status, _, err = _run(capsys, "changed-trusted", site, site, "--pair", pair)

    assert (status, err) == (
        EX_CONFIG,
        f"creche-env: {pair / 'root-keys.txt'}:2: unknown-key SURPLUS\n",
    )


@pytest.mark.parametrize(
    ("text", "fault"),
    [
        ("TRUSTED\nTRUSTED\n", "duplicate-key TRUSTED"),
        ("TRUSTED\n\n", "bad-key"),
        ("x\n", "bad-key"),
    ],
)
def test_root_keys_hold_one_key_on_each_line(
    capsys: pytest.CaptureFixture[str], pair: Path, site: Path, text: str, fault: str
) -> None:
    _write(pair / "root-keys.txt", text, 0o644)

    _, _, err = _run(capsys, "changed-trusted", site, site, "--pair", pair)

    assert err.endswith(f": {fault}\n")


def test_raw_prints_one_value_and_nothing_for_an_absent_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    hooks = _write(tmp_path / "hooks.env", "TOKEN=a=b\nURL=http://127.0.0.1:1/x\n")

    assert _run(capsys, "raw", hooks, "TOKEN") == (0, "a=b\n", "")
    assert _run(capsys, "raw", hooks, "ABSENT") == (0, "", "")
    assert _run(capsys, "raw", hooks, "not a key") == (
        EX_CONFIG,
        "",
        f"creche-env: {hooks}: bad-key\n",
    )


def test_raw_refuses_a_reference(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    hooks = _write(tmp_path / "hooks.env", "URL=${OTHER}\n")

    assert _run(capsys, "raw", hooks, "URL") == (
        EX_CONFIG,
        "",
        f"creche-env: {hooks}:1: reference-in-env URL\n",
    )
