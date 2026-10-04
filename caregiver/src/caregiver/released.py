"""The image references a `playpen` release installed.

    /opt/components/playpen/images.env      root wrote it, at step 8
        base=<registry>/<name>@sha256:…     (`playpen/bin/playpen-build`)
        python=<registry>/<name>@sha256:…
             |
             v
    ReleasedImages.current(given)           one read per pass
             |
             v
    SandboxImages                           what `sbx create -t` is given

Until this module the references came from two flags, written into the
unit's environment once, by an installer. A `playpen` release then swapped
a tree nothing read, and the fleet stayed on the image the installer built.

Five rules, each with the reason it is a rule.

1. **A released file wins over the flags.** From a component's first
   release its tree is the executor's, and the tap approved the bytes that
   tree names. The flags are what a host with no `playpen` release runs on.
2. **A file that is absent, or that does not read, keeps the LAST GOOD
   answer.** A switch renames the tree away for a moment. Read as "no
   release", that moment would move every family onto the flags' image and
   back, one sandbox replacement each way. Only a process that has never
   read a released file answers with the flags.
3. **A reference is a digest, never a tag.** A tag can move, and the tap
   approved exact bytes.
4. **The flavors are a closed set, and `base` is required.** A line this
   module does not know refuses the whole file: a half-read file is a
   fleet on two releases.
5. **The file is read as input.** No symlink is followed, the read is
   capped, and every line matches one pattern in full. The tree is root's,
   so this is the second fence and not the first.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
from pathlib import Path
from typing import Final

from agent_family import SandboxFlavor

from .images import SandboxImages

log = logging.getLogger("caregiver.released")

#: Where a `playpen` release puts the file (`playpen/component.yaml`,
#: `install.to`). `handover` owns the install root, and the two packages
#: share no module, so the path is spelled here a second time.
RELEASED_IMAGES: Final = Path("/opt/components/playpen/images.env")

#: Two lines of under two hundred characters. A larger file is not one.
MAX_BYTES: Final = 4096

#: `<registry>/<name>@sha256:<64 hex>`. `playpen/bin/playpen-verify` holds
#: the same shape, and `bin/tests/test_playpen_release.py` reads a file the
#: build wrote through this module's parser.
REFERENCE_RE: Final = re.compile(r"[a-z0-9.:-]+/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}")

ASSIGN: Final = "="

_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def parse_images(text: str) -> SandboxImages | None:
    """One `SandboxImages`, or None for a file that is not one (rules 3, 4)."""
    found: dict[str, str] = {}
    known = {str(flavor) for flavor in SandboxFlavor}
    for line in text.splitlines():
        if not line:
            continue

        flavor, assigned, reference = line.partition(ASSIGN)
        if not assigned or flavor not in known or flavor in found:
            return None

        if REFERENCE_RE.fullmatch(reference) is None:
            return None

        found[flavor] = reference

    base = found.get(str(SandboxFlavor.BASE))
    if base is None:
        return None

    return SandboxImages(base=base, python=found.get(str(SandboxFlavor.PYTHON), ""))


def read_images(path: Path) -> SandboxImages | None:
    """The file at `path`, or None when there is none to believe (rule 5)."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError:
        return None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            return None

        raw = os.read(fd, MAX_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)

    try:
        return parse_images(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None


class ReleasedImages:
    """What this process last read out of the released file (rule 2).

    One object for the life of the process. Passes run in threads, so the
    read and the memory sit behind one lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._last: SandboxImages | None = None

    def current(self, given: SandboxImages) -> SandboxImages:
        """The released references, or `given` when no release was ever read."""
        with self._lock:
            found = read_images(self._path)
            if found is not None and found != self._last:
                log.info(
                    "caregiver: sandbox images from %s: base=%s python=%s",
                    self._path,
                    found.base,
                    found.python or "(none)",
                )
                self._last = found

            return self._last if self._last is not None else given
