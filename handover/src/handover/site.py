"""The site: what differs from one deployment of this code to the next.

This repository names no host, no account and no owner. Whoever runs a
deployment writes those into ONE file, and everything here that must know
one reads it from there:

    /etc/creche/site.env          KEY=VALUE lines, `#` comments

        AGENT_GITHUB_OWNER=example-owner     who owns the repositories a
                                             release is read from
        AGENT_OPERATOR_USER=operator         the account the user units run
                                             as
        AGENT_OPERATOR_HOME=/home/operator   that account's home, which holds
                                             the user units and their trees
        AGENT_LAN_ADDRESS=192.0.2.10         the host's LAN address, which
                                             the intake binds and links to
        AGENT_GITHUB_REPO_AGENT_CONTROL=...  optional, one per repository:
                                             its name on GitHub, when that
                                             is not the catalog's name

The same file is every unit's `EnvironmentFile`, and that is how a service
reads the values it needs. This module reads only what root's executor, the
requester and the intake need, because none of those is started with the
file in its environment.

The file holds no secret. It is root-owned and world-readable, because root's
executor, the operator's requester and the PEP's release door all read it,
and each must get the same answer.

A value that is missing or malformed refuses with the `site` code. It never
falls back to a default: a default would be somebody's host.

`AGENT_SITE_FILE` names another file. The tests set it, and nothing on a
host does.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Final

from .errors import Refusal, RefusalCode, safe_token

#: Where a deployment writes its values.
SITE_FILE: Final = Path("/etc/creche/site.env")

#: Names another file. Read on every call, so a test can move it.
SITE_FILE_ENV: Final = "AGENT_SITE_FILE"

#: A site file is a handful of lines. Past this it is not one.
MAX_SITE_BYTES: Final = 64 * 1024

COMMENT: Final = "#"
ASSIGN: Final = "="

#: The two quote characters systemd strips from an `EnvironmentFile` value.
QUOTES: Final = ('"', "'")

#: The write bits of group and other. A site file that carries either is a
#: file somebody else can change.
OTHERS_WRITE: Final = stat.S_IWGRP | stat.S_IWOTH

#: Root's uid.
ROOT_UID: Final = 0

#: The one account the operator can never be.
ROOT_USER: Final = "root"


class SiteKey(StrEnum):
    """Every value this package reads from the site file."""

    GITHUB_OWNER = "AGENT_GITHUB_OWNER"
    OPERATOR_USER = "AGENT_OPERATOR_USER"
    OPERATOR_HOME = "AGENT_OPERATOR_HOME"
    LAN_ADDRESS = "AGENT_LAN_ADDRESS"


#: One DNS label: letters, digits and inner hyphens, at most 63 characters.
_LABEL: Final = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"

#: What each value may look like. A GitHub login is 1 to 39 characters of
#: letters, digits and single hyphens, and never starts or ends with one. A
#: Linux account name is what `useradd` accepts by default. A home is an
#: absolute path of plain segments: it is joined into install paths, so `..`
#: and a trailing slash are refused where it is read. A LAN address is an
#: IPv4 address or a host name, both dot-separated plain labels: root binds
#: it and builds a link from it, so a port, a scheme or a slash is refused.
_SHAPES: Final[dict[SiteKey, re.Pattern[str]]] = {
    SiteKey.GITHUB_OWNER: re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"),
    SiteKey.OPERATOR_USER: re.compile(r"[a-z_][a-z0-9_-]{0,31}"),
    SiteKey.OPERATOR_HOME: re.compile(r"(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)+"),
    SiteKey.LAN_ADDRESS: re.compile(rf"{_LABEL}(?:\.{_LABEL})*"),
}


#: Starts the optional key that renames one repository on GitHub. The rest
#: of the key is the catalog's name for it, in capitals with `_` for `-`:
#: `agent-control` is `AGENT_GITHUB_REPO_AGENT_CONTROL`.
REPO_KEY_PREFIX: Final = "AGENT_GITHUB_REPO_"

#: A GitHub repository name: letters, digits, `.`, `_` and `-`, at most 100
#: characters. It is spliced into an API path, so it never starts with `.`
#: or `-`, which rules out `.` and `..` as well.
_REPO_NAME: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")


def site_file() -> Path:
    """The file to read: `AGENT_SITE_FILE` when set, else `SITE_FILE`."""
    named = os.environ.get(SITE_FILE_ENV)

    return Path(named) if named else SITE_FILE


def _refuse(detail: str) -> Refusal:
    return Refusal(RefusalCode.SITE, "site file", detail)


@cache
def _values(path: Path) -> dict[str, str]:
    """Every `KEY=VALUE` line of `path`. The last line of a key wins."""
    try:
        text = _read_judged(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise _refuse(f"{path} cannot be read: {type(exc).__name__}") from exc

    found: dict[str, str] = {}
    for line in text.splitlines():
        bare = line.strip()
        if not bare or bare.startswith(COMMENT):
            continue

        key, sign, value = bare.partition(ASSIGN)
        if not sign:
            raise _refuse(f"{path} holds a line that is not KEY=VALUE")

        found[key.strip()] = _unquoted(value.strip())

    return found


def _read_judged(path: Path) -> str:
    """The text of `path`, judged and read through ONE open.

    `stat` and then `read_text` would resolve the path twice, and the file
    that was judged need not be the file that is read. The descriptor is one
    file for as long as it is open, whatever the path names by then.
    """
    with os.fdopen(os.open(path, os.O_RDONLY), "rb") as handle:
        info = os.fstat(handle.fileno())
        _require_trusted(path, info)
        if info.st_size > MAX_SITE_BYTES:
            raise _refuse(f"{path} is larger than {MAX_SITE_BYTES} bytes")

        return handle.read(MAX_SITE_BYTES).decode("utf-8")


def _unquoted(value: str) -> str:
    """`"x"` and `'x'` as `x`, which is what systemd hands a unit from the
    same line. One pair, and only a matching one."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in QUOTES:
        return value[1:-1]

    return value


def _require_trusted(path: Path, info: os.stat_result) -> None:
    """Refuse a site file somebody else could have written.

    The file says which account root drops to and where root installs, so
    whoever can write it chooses both. Its owner must be root, or the
    account that reads it (a test, a developer's own file), and neither
    group nor other may write it. For root that leaves root alone. `path`
    is followed: a link's target is what is judged.
    """
    if info.st_uid not in (ROOT_UID, os.getuid()):
        raise _refuse(f"{path} is owned by uid {info.st_uid}, not by root")

    if info.st_mode & OTHERS_WRITE:
        raise _refuse(f"{path} is writable by group or other")


def read(key: SiteKey) -> str:
    """One value of the site file, checked against its shape."""
    path = site_file()
    value = _values(path).get(key.value)
    if value is None:
        raise _refuse(f"{path} sets no {key.value}")

    if not _SHAPES[key].fullmatch(value):
        raise _refuse(f"{key.value} is not a value this accepts: {safe_token(value)}")

    return value


def forget() -> None:
    """Drop what was read, so the next `read` opens the file again. A
    long-lived process calls it where it re-reads its other inputs."""
    _values.cache_clear()


def github_owner() -> str:
    """Who owns the repositories a release is read from."""
    return read(SiteKey.GITHUB_OWNER)


def github_repo(name: str) -> str:
    """What GitHub calls the repository the catalog calls `name`.

    The catalog's names are this code's own. A site that hosts a repository
    under another name says so, and one that does not is asked by the
    catalog's name.
    """
    key = REPO_KEY_PREFIX + name.upper().replace("-", "_")
    value = _values(site_file()).get(key)
    if value is None:
        return name

    if not _REPO_NAME.fullmatch(value):
        raise _refuse(f"{key} is not a value this accepts: {safe_token(value)}")

    return value


def operator_user() -> str:
    """The account the user units run as. Never `root`: a release that
    installed the operator's trees as root would hand every service root's
    files."""
    name = read(SiteKey.OPERATOR_USER)
    if name == ROOT_USER:
        raise _refuse(f"{SiteKey.OPERATOR_USER.value} names root")

    return name


def operator_home() -> str:
    """The operator's home. Their user units live under it, and so do the
    trees those units run from."""
    return read(SiteKey.OPERATOR_HOME)


def require_complete() -> None:
    """Everything the executor needs before it opens its spool, so what
    would stop it there refuses here. The executor's own verify hook calls
    it (`check --site`): a new executor that could not start on this host
    fails the hook while the old one is still there to put back."""
    github_owner()
    operator_account()
    lan_address()


def operator_account() -> pwd.struct_passwd:
    """The site's operator, as this host's password database knows it.

    Two things the site file cannot prove alone. The account exists here.
    And its home is the one the site names: systemd expands `%h` in a user
    unit to the database's home, and the executor judges such a unit
    against the site's, so two different directories would approve a path
    systemd never runs. The database may spell the home with a trailing
    slash, which is the same directory.
    """
    # Both site values first: a missing one is the clearer refusal.
    name, home = operator_user(), operator_home()
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        detail = f"{SiteKey.OPERATOR_USER.value} names {name}, which is no account of this host"
        raise _refuse(detail) from None

    if os.path.normpath(entry.pw_dir) != home:
        raise _refuse(f"{SiteKey.OPERATOR_HOME.value} is {home}, and the account's home is not")

    return entry


def lan_address() -> str:
    """The host's LAN address. The intake binds it and the link the
    operator taps names it."""
    return read(SiteKey.LAN_ADDRESS)
