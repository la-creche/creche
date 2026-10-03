"""The gap directory as ROOT reads it (`stage7-releases.md` §4.3 step 1).

```
/srv/agents/state/rework/secret-gaps/<name>.json   written by the operator  <- HOSTILE
```

`caregiver` opens a gap for every declared secret that has no value
(`mcp_release.py`), and this module reads them: without it §4.1's five
automatic steps end at step 3.

The directory is a SIBLING of the secrets directory and never a child of
it. It has to be operator-writable, because operator-side code writes it. The
secrets directory next door must not be, because sealing needs public keys
only: any writer of it can author a `<name>.enc` and pre-empt the name the operator
is about to fill.

So every byte here is hostile, and the rules are §3.2's — the same ones
`spool.py` applies to `requests/`, because the threat is the same one.

1. The directory is opened ONCE, `O_DIRECTORY | O_NOFOLLOW`, and every
   read, stat and unlink after that is relative to that one descriptor. A
   rename of the directory mid-pass cannot move root's reads elsewhere.
2. A gap file is opened `O_NOFOLLOW | O_NONBLOCK`, must be a regular file,
   owned by the writer this directory belongs to, and at most
   `MAX_GAP_BYTES`.
3. **Names come from the grammar only.** A name that is not
   `<SECRET_NAME_RE>.json` is never opened. The stem becomes a file name
   under ROOT's secrets directory two steps later, so a name root cannot
   read is a name root does not carry forward.
4. The key set is closed, and the body must AGREE with the file name. A
   file called `weather_token.json` whose body names `github_token` is
   refused whole: otherwise the gap the operator is shown and the gap root fills
   are two different names, which is §6 row 8 through the side door.
5. The pass is bounded. The directory's size is the attacker's choice, so
   root reads a bounded prefix with `scandir`, which yields lazily, and
   sorts only what it kept.
6. `at` is read and decides NOTHING. It is an operator-written timestamp, and
   one dated in the future holds any decision it gets a vote in for ever.

**Root refuses one entry, never the directory.** One planted file must not
hide a real gap, or the attacker stops every future MCP server with one
write.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from .token import SECRET_NAME_RE, SERVER_NAME_RE

#: `caregiver`'s `GAPS_DIR`, a SIBLING of the secrets directory.
DEFAULT_GAPS_DIR: Final = "/srv/agents/state/rework/secret-gaps"

GAP_SUFFIX: Final = ".json"

#: A gap is three short fields. `caregiver`'s own writer produces well under
#: 200 bytes, and 4 KiB is `spool.py`'s cap for a whole release request.
MAX_GAP_BYTES: Final = 4096

#: How many entries one pass reads. The roster of MCP servers is small and
#: `token.MAX_PENDING_TOKENS` is 64, so a pass that read more could mint
#: nothing with what it found.
MAX_GAPS_PER_PASS: Final = 64

#: How many directory entries root will even look at before it stops
#: looking. A flood of names that fail rule 3 costs one `fullmatch` each,
#: and this is what bounds that.
MAX_SCANNED_ENTRIES: Final = 4096

#: §3.2 rule 2 applied to this file: an unknown key is a refusal.
GAP_KEYS: Final = frozenset({"server", "secret", "at"})

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass(frozen=True)
class OpenGap:
    """One secret root may mint a token for.

    `server` and `secret` have both passed their patterns, which is what
    lets `Tokens.mint` accept them and what lets the form page name them.
    `file_name` is root's own construction — `secret` plus the suffix — and
    never a byte off the disk.
    """

    server: str
    secret: str
    #: The writer's timestamp. Recorded for the journal. It decides
    #: nothing, so a value in the future costs nothing (rule 6).
    at: float

    @property
    def file_name(self) -> str:
        return f"{self.secret}{GAP_SUFFIX}"


class GapDirectory:
    """Root's read of one operator-written directory.

    Every method opens the directory afresh. Holding a descriptor open
    across a whole run would be faster and would also keep a deleted
    directory alive for as long as the intake runs, which reads as "the
    gap is still open" long after `caregiver` removed it.
    """

    def __init__(self, directory: Path, writer_uid: int) -> None:
        self._directory = directory
        self._writer_uid = writer_uid

    def open_gaps(self) -> tuple[OpenGap, ...]:
        """Every gap root will carry forward, in name order.

        An unreadable directory answers empty, which is the same answer an
        empty one gives. Root mints nothing either way.
        """
        fd = self._open_directory()
        if fd is None:
            return ()

        try:
            found = [self._read_one(fd, name) for name in self._names(fd)]
        finally:
            os.close(fd)

        return tuple(sorted((one for one in found if one), key=lambda gap: gap.secret))

    def close(self, gap: OpenGap) -> bool:
        """The gap is filled. Remove the file so nobody re-opens it.

        True when a file went away. `caregiver` will not write it again,
        because `_gaps_for` skips a name whose `<name>.enc` exists — and
        that is checked in the right order, because root closes the gap
        only after the value has landed.
        """
        return self.close_named(gap.secret)

    def close_named(self, secret: str) -> bool:
        """The same, for a caller that holds the NAME and no record.

        `Intake` is that caller: it learns the name from a token it
        minted, so the name passed `SECRET_NAME_RE` before the token
        existed. The pattern is checked again anyway, because a file name
        built from an unchecked string is the whole of rule 3.
        """
        if SECRET_NAME_RE.fullmatch(secret) is None:
            return False

        fd = self._open_directory()
        if fd is None:
            return False

        try:
            os.unlink(f"{secret}{GAP_SUFFIX}", dir_fd=fd)
        except OSError:
            return False
        finally:
            os.close(fd)

        return True

    def _open_directory(self) -> int | None:
        """Rule 1. `O_NOFOLLOW` refuses a symlink planted where the
        directory goes, which no flag on the FILE open could see."""
        try:
            return os.open(self._directory, _DIR_FLAGS)
        except OSError:
            return None

    def _names(self, dir_fd: int) -> list[str]:
        """Rule 5. A bounded prefix of the entries whose names root will
        read at all. `scandir` yields lazily, so nothing is materialized
        whole, and the budget is spent on entries rather than on matches."""
        kept: list[str] = []
        scanned = 0
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCANNED_ENTRIES or len(kept) >= MAX_GAPS_PER_PASS:
                    break

                if _secret_of(entry.name) is not None:
                    kept.append(entry.name)

        return kept

    def _read_one(self, dir_fd: int, name: str) -> OpenGap | None:
        """One entry, or None. None is every refusal, and the caller drops
        it without a word: the only thing root could say about a file it
        would not read is the file's own name."""
        secret = _secret_of(name)
        if secret is None:
            return None

        raw = _read_capped(dir_fd, name, self._writer_uid)
        if raw is None:
            return None

        return _parse_gap(raw, secret)


def _secret_of(name: str) -> str | None:
    """Rule 3. The secret name inside a gap file name, or None.

    `fullmatch` and not `match`: the stem becomes a file name under root's
    secrets directory, and a pattern that stops early is how `weather_token`
    and `weather_token/../../etc` become the same name.
    """
    if not name.endswith(GAP_SUFFIX):
        return None

    secret = name[: -len(GAP_SUFFIX)]

    return secret if SECRET_NAME_RE.fullmatch(secret) else None


def _read_capped(dir_fd: int, name: str, writer_uid: int) -> bytes | None:
    """Rule 2. One file's bytes, or None.

    `O_NOFOLLOW` refuses a symlink, `O_NONBLOCK` keeps a FIFO from holding
    root for ever, and `fstat` refuses everything that is not a plain file
    the expected writer owns.
    """
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != writer_uid:
            return None

        if info.st_size > MAX_GAP_BYTES:
            return None

        # To EOF but never past the cap: a file that grew after the fstat
        # is still caught, and one read() may answer short.
        raw = b""
        while len(raw) <= MAX_GAP_BYTES and (chunk := os.read(fd, MAX_GAP_BYTES + 1 - len(raw))):
            raw += chunk
    except OSError:
        return None
    finally:
        os.close(fd)

    return None if len(raw) > MAX_GAP_BYTES else raw


def _parse_gap(raw: bytes, secret: str) -> OpenGap | None:
    """Rule 4. The three fields, the closed key set, and the agreement
    between the body and the file name."""
    try:
        loaded: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None

    if not isinstance(loaded, dict):
        return None

    fields = cast("dict[str, object]", loaded)
    if not set(fields) <= GAP_KEYS:
        return None

    server = fields.get("server")
    named = fields.get("secret")
    if not isinstance(server, str) or SERVER_NAME_RE.fullmatch(server) is None:
        return None

    # The body must name the same secret as the file. Two names for one
    # gap is §6 row 8 through the side door: the operator is shown one and root
    # fills the other.
    if named != secret:
        return None

    at = fields.get("at")

    return OpenGap(server, secret, float(at) if isinstance(at, int | float) else 0.0)
