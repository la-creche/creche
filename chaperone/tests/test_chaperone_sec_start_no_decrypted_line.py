"""A secrets file that will not parse never puts a decrypted line in the journal.

PyYAML's error message quotes the line it failed on. The PEP decrypts
the `PEP_SECRETS` file at start, so unhandled, a hand edit that broke a
quote would send the traceback, and one decrypted line, to
`journalctl -u creche-chaperone`. `UnicodeDecodeError` does the same with a byte.

    sops -d --> text --> SafeLoader --> {name: value}
                 |           |
                 |           `--> "not valid YAML at line N, column M"
                 `--> "not UTF-8 at byte N"

Every error names the file and a position and nothing of the content.
The start cases run the real entry point in a child, because the journal
gets the child's whole stderr: the log line and any traceback.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from chaperone.secrets import SecretsFormatError, load_secret_dir, load_sops_secrets

pytestmark = pytest.mark.slow

#: What the decrypted file holds where it breaks. It must reach no output.
LEAK: Final = "LEAK-9f3c2e71"

#: `UnicodeDecodeError` quotes the byte it failed on, as `0xff`.
LEAK_BYTE: Final = b"\xff"
LEAK_BYTE_TEXT: Final = "0xff"

#: What the operator runs to fix the monolith.
#: It names no path: the file is the site's, and the line names it already.
MONOLITH_FIX: Final = "Fix it with sops"

#: A start that is still running after this has not stopped.
START_TIMEOUT_S: Final = 30


@pytest.fixture
def fake_sops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `sops` that prints the file it is asked to decrypt, as
    `test_chaperone_prl_reload_family_path.py` fakes it. The real one needs a key."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "sops"
    stub.write_text('#!/bin/sh\nexec cat "$2"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")

    return binaries


def _start(tmp_path: Path, **pep_env: str) -> subprocess.CompletedProcess[str]:
    """`python -m chaperone` as the unit starts it, with only `pep_env` set."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("PEP_")}
    env |= {
        "PEP_REWORK_DIR": str(tmp_path / "rework"),
        "PEP_AUDIT_DIR": str(tmp_path / "audit"),
        "PEP_BIND": "127.0.0.1:0",
        **pep_env,
    }

    return subprocess.run(
        [sys.executable, "-m", "chaperone"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=START_TIMEOUT_S,
    )


#: A monolith whose merge keys copy one mapping of 256 names 257 times: 256
#: more pairs than the reader takes (`bounded_yaml`). It is the one case of
#: `NOT_YAML` that no hand edit makes.
PAST_THE_MERGE_LIMIT: Final = (
    f"base: &a {{{', '.join(f'k{n}: {LEAK}' for n in range(256))}}}\n"
    f"<<: [{', '.join(['*a'] * 257)}]\n"
)

#: A decrypted monolith that will not parse, and where it breaks. Each one
#: is a real way a hand edit goes wrong.
NOT_YAML: Final = (
    pytest.param(
        f'kagi_api_key: abc\nsmtp_password: "{LEAK}\n',
        "not valid YAML at line 3, column 1 (in what starts at line 2, column 16)",
        id="unclosed-quote",
    ),
    pytest.param(
        f"kagi_api_key: sk-{LEAK}: pasted\n",
        "not valid YAML at line 1, column 31",
        id="stray-colon",
    ),
    pytest.param(
        f"kagi_api_key: abc\n\tsmtp_password: {LEAK}\n",
        "not valid YAML at line 2, column 1",
        id="tab-indent",
    ),
    pytest.param(
        f"kagi_api_key: !!python/object:os.system {LEAK}\n",
        "not valid YAML at line 1, column 15",
        id="unknown-tag",
    ),
    pytest.param(
        f"kagi_api_key: [{LEAK}\n",
        "not valid YAML at line 2, column 1",
        id="unclosed-flow",
    ),
    pytest.param(
        f"kagi_api_key: {LEAK}\x07\n",
        "not valid YAML at line 1, column 28",
        id="control-character",
    ),
    pytest.param(
        f"{LEAK}: 2001-02-30\n",
        "a value YAML cannot build at line 1, column 16",
        id="impossible-date",
    ),
    pytest.param(
        f"kagi_api_key: {'[' * 5000}{LEAK}\n",
        "nested deeper than YAML can parse",
        id="deep-nesting",
    ),
    pytest.param(
        PAST_THE_MERGE_LIMIT,
        "merge keys past a limit at line 1, column 7",
        id="merge-keys-past-a-limit",
    ),
)


# -- start: the PEP stops, and says where -----------------------------------


@pytest.mark.usefixtures("fake_sops")
@pytest.mark.parametrize(("decrypted", "where"), NOT_YAML)
def test_start_stops_on_a_monolith_that_is_not_yaml(
    tmp_path: Path, decrypted: str, where: str
) -> None:
    monolith = tmp_path / "secrets.enc.yaml"
    monolith.write_text(decrypted, encoding="utf-8")

    done = _start(tmp_path, PEP_SECRETS=str(monolith))

    assert done.returncode == os.EX_CONFIG, done.stderr
    assert LEAK not in done.stderr + done.stdout
    assert "Traceback" not in done.stderr
    assert f"{monolith}: {where}" in done.stderr
    assert MONOLITH_FIX in done.stderr


@pytest.mark.usefixtures("fake_sops")
@pytest.mark.parametrize(
    ("decrypted", "shape"),
    (
        pytest.param(f"- {LEAK}\n", "a sequence", id="list"),
        pytest.param(f"{LEAK}\n", "a scalar", id="scalar"),
        pytest.param("", "empty", id="empty"),
    ),
)
def test_start_stops_on_a_monolith_that_is_not_a_mapping(
    tmp_path: Path, decrypted: str, shape: str
) -> None:
    """The shape and never the content. Starting with no credential at all
    would serve nothing useful either."""
    monolith = tmp_path / "secrets.enc.yaml"
    monolith.write_text(decrypted, encoding="utf-8")

    done = _start(tmp_path, PEP_SECRETS=str(monolith))

    assert done.returncode == os.EX_CONFIG, done.stderr
    assert LEAK not in done.stderr + done.stdout
    assert f"{monolith}: {shape}, not a mapping" in done.stderr
    assert MONOLITH_FIX in done.stderr


@pytest.mark.usefixtures("fake_sops")
def test_start_stops_on_a_monolith_that_is_not_utf8(tmp_path: Path) -> None:
    monolith = tmp_path / "secrets.enc.yaml"
    monolith.write_bytes(b"kagi_api_key: abc" + LEAK_BYTE + b"\n")

    done = _start(tmp_path, PEP_SECRETS=str(monolith))

    assert done.returncode == os.EX_CONFIG, done.stderr
    assert LEAK_BYTE_TEXT not in done.stderr + done.stdout
    assert f"{monolith}: not UTF-8 at byte 17" in done.stderr


@pytest.mark.usefixtures("fake_sops")
def test_start_stops_on_a_pasted_value_that_is_not_utf8(tmp_path: Path) -> None:
    """The per-secret store. The intake seals UTF-8 only, so this file had
    another writer."""
    store = tmp_path / "secrets"
    store.mkdir()
    sealed = store / "kagi_api_key.enc"
    sealed.write_bytes(b"ab" + LEAK_BYTE)

    done = _start(tmp_path, PEP_SECRETS_DIR=str(store))

    assert done.returncode == os.EX_CONFIG, done.stderr
    assert LEAK_BYTE_TEXT not in done.stderr + done.stdout
    assert "Traceback" not in done.stderr
    assert f"{sealed}: not UTF-8 at byte 2" in done.stderr


# -- the loader: nothing of the content, even in a traceback ----------------


@pytest.mark.usefixtures("fake_sops")
@pytest.mark.parametrize(("decrypted", "where"), NOT_YAML)
def test_the_error_carries_no_content_and_chains_none(
    tmp_path: Path, decrypted: str, where: str
) -> None:
    """A caller that logs with `exc_info`, or a raise that leaves `main`,
    prints the chain too. The YAML error must not be in it."""
    monolith = tmp_path / "secrets.enc.yaml"
    monolith.write_text(decrypted, encoding="utf-8")

    with pytest.raises(SecretsFormatError) as caught:
        load_sops_secrets(monolith)

    assert where in str(caught.value)
    assert LEAK not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.usefixtures("fake_sops")
def test_a_pasted_value_error_chains_none(tmp_path: Path) -> None:
    store = tmp_path / "secrets"
    store.mkdir()
    (store / "kagi_api_key.enc").write_bytes(LEAK_BYTE)

    with pytest.raises(SecretsFormatError) as caught:
        load_secret_dir(store)

    assert LEAK_BYTE_TEXT not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.usefixtures("fake_sops")
def test_a_dropped_entry_names_its_line_and_never_its_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A value pasted with a stray colon on a line of its own is a KEY with
    no value. Naming the key would log the secret."""
    monolith = tmp_path / "secrets.enc.yaml"
    monolith.write_text(f"kagi_api_key: abc\n{LEAK}:\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        found = load_sops_secrets(monolith)

    assert found == {"kagi_api_key": "abc"}
    assert LEAK not in caplog.text
    assert f"{monolith}: line 2: dropping an entry" in caplog.text
