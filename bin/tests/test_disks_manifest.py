"""The verify timeout of the disks row against the digests follow of the vmm.

Decision CH77 rule 2, caller duty 13 of `rust/crates/vmm/AGENTS.md`: the
vmm imports new bytes of `digests.json` only after they stay the same for
`follow::HOOK_TIMEOUT` plus one poll. That wait covers the disks verify
hook `vmm check-images` only when the hook's own bound,
`verify.timeout_s` of `disks/component.toml`, equals `HOOK_TIMEOUT`. A
longer timeout lets the hook still run when the follow imports. So this
test reads both sources and holds them equal.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from handover.discovery import read_one

REPO: Final = Path(__file__).resolve().parents[2]
FOLLOW: Final = REPO / "rust" / "crates" / "vmm" / "src" / "image_root" / "follow.rs"

#: The one line of follow.rs that sets the constant. The match takes
#: whole seconds only, so a change to another unit fails here.
HOOK_TIMEOUT_RE: Final = re.compile(
    r"^pub const HOOK_TIMEOUT: Duration = Duration::from_secs\((\d+)\);$", re.MULTILINE
)


def _hook_timeout_s() -> int:
    """The seconds of `follow::HOOK_TIMEOUT`, read from the Rust source."""
    found = HOOK_TIMEOUT_RE.findall(FOLLOW.read_text(encoding="utf-8"))
    assert len(found) == 1, f"{FOLLOW}: expected one HOOK_TIMEOUT line, found {len(found)}"

    return int(found[0])


def test_the_disks_verify_timeout_is_the_follow_hook_timeout() -> None:
    """`verify.timeout_s` of the disks row equals `follow::HOOK_TIMEOUT`."""
    manifest = read_one(REPO, "disks").manifest

    assert manifest.verify.timeout_s == _hook_timeout_s()


def test_the_hook_timeout_is_300_s() -> None:
    """The value of decision CH77 rule 2 and gap Q68-6: 300 s."""
    assert _hook_timeout_s() == 300
