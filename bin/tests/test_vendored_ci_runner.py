"""The vendored crates of ci-runner stay copies of one commit.

`rust/vendor/ci-runner/` holds `ci-runner-peer` and `ci-runner-sys` of
commit 131ce43 of the repository infra/ci-runner, byte for byte
(`rust/vendor/ci-runner/SOURCE.md`). A file that changes by hand fails
here. A new copy changes the list below and SOURCE.md in one commit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
VENDOR: Final = REPO / "rust" / "vendor" / "ci-runner"

#: The commit of the copy, as SOURCE.md names it.
COMMIT: Final = "131ce43acd5185cdc40440ead64357aa03300bdb"

#: The SHA-256 of each file of the copy, by its path under `crates/`.
FILES: Final = {
    "crates/ci-runner-peer/AGENTS.md": (
        "1cba3f936d2a37002f0719e7504aeec9d5f516a12cf071ead0a3585cd41ade46"
    ),
    "crates/ci-runner-peer/Cargo.toml": (
        "6cdbf1aa371f3655c12f263bd8fe2dd1aebfcf12e21485a26b369df8da08ab93"
    ),
    "crates/ci-runner-peer/README.md": (
        "f750c18317544e9f43d6c60facce10657f6d3ecb5baf7ee1dfff21459f89d6a6"
    ),
    "crates/ci-runner-peer/src/check.rs": (
        "d0276e8994973847bce28bfb357365c827b6884b58d141cdd5929eb446e93e43"
    ),
    "crates/ci-runner-peer/src/daemon.rs": (
        "ab463c24abb5eb979d5b1710e8a424599613b34701284f35cb60bc1feffcce49"
    ),
    "crates/ci-runner-peer/src/lib.rs": (
        "0843d17bf370839bec4a2bc76831b652c795cd04b58031808f6c22d9bb59a999"
    ),
    "crates/ci-runner-peer/src/unit.rs": (
        "63398ab2a71e8bff244b47f686d3f60634b971d135eb5af77a5aa4c072c109ad"
    ),
    "crates/ci-runner-sys/Cargo.toml": (
        "95e30c7960a48d10c6b0cdddd261f859074f14eefccd30bd33ca4e32a8895570"
    ),
    "crates/ci-runner-sys/src/lib.rs": (
        "b9f63c52cd0a7c29021943b7065bb14b07f03c9e5f525cec4e84749e2ae42a6f"
    ),
}


def test_each_file_is_the_copy_of_the_commit() -> None:
    for name, digest in FILES.items():
        data = (VENDOR / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name


def test_the_copy_holds_no_other_file() -> None:
    found = {
        path.relative_to(VENDOR).as_posix()
        for path in (VENDOR / "crates").rglob("*")
        if path.is_file()
    }
    assert found == set(FILES)


def test_source_names_the_commit() -> None:
    assert COMMIT in (VENDOR / "SOURCE.md").read_text(encoding="utf-8")
