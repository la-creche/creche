"""The component's verify hook (contract 06 §4).

`view/component.yaml` names `bin/view-verify --json`, and §4 rule 6 says
the hook ships inside the component's own artifact. This is that hook.

It answers one question: does this component work right now? Exit 0 is a
pass and anything else is a failure (§4 rule 3). It is read-only, writes
nothing anywhere, and the only call it makes is to this host's own
`/healthz` on the address the service was configured to bind (§4 rules 1
and 2).

It is runnable at any time, not only after a release, because the
platform doctor calls it too (§4 rule 5).

It never prints the access key, the `view-ro` token or any part of
either. `/healthz` is the keyless path, so the hook needs no secret to
make its one call (invariant 13).

**The environment.** Contract 06 §4 rule 7: root's executor hands every
hook a small, fixed environment (`stage7-releases.md` §2.4) that carries
none of `agent-view.service`'s own `EnvironmentFile=`. Without
`--env-file`, that means no bind and no access key, and `from_env`
correctly refuses to serve an unkeyed admin surface on the LAN — the
right answer to the wrong question, on every release. `--env-file <path>`
names the SAME file the unit's own
`EnvironmentFile=` does (`view/component.yaml`'s `verify.command`, pinned
to the unit by `release/tests/test_release_r7r_verify_env.py`), and this
hook reads it itself, as its own user — never root, which is what keeps a
root process from ever opening a file only the operator's account controls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from .config import Config, ConfigError, from_env

EXIT_OK = 0
EXIT_FAILED = 1

#: §4 rule 4 caps the captured output, so the hook stays small by design.
HEALTH_TIMEOUT_S: Final = 5.0

#: A shell variable name (POSIX `Name`), which is what `envfile_upsert`
#: (`bin/lib/envfile.sh`) writes to the left of `=`.
_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def main(argv: list[str] | None = None) -> int:
    """The `view-verify` entry point."""
    parsed = _parse_args(argv if argv is not None else sys.argv[1:])

    try:
        source = _environ(parsed.env_file)
    except OSError as error:
        return _report([Check("env-file", False, str(error))], as_json=parsed.json)

    try:
        config = from_env(source)
    except ConfigError as error:
        return _report([Check("config", False, str(error))], as_json=parsed.json)

    return _report(_checks(config), as_json=parsed.json)


def _environ(env_file: Path | None) -> dict[str, str]:
    """What this process reads its configuration from: whatever it
    inherited, with `--env-file` layered on top.

    The file wins over an inherited value on purpose: it is what the unit
    would have given this process, and the inherited environment is only
    ever root's own fixed one (the docstring's "The environment").
    """
    merged = dict(os.environ)
    if env_file is not None:
        merged.update(_parse_env_file(env_file))

    return merged


def _parse_env_file(path: Path) -> dict[str, str]:
    """A shell-free reader for a systemd `EnvironmentFile=`. One
    `KEY=value` per line, blank lines and `#` comments skipped, no quoting
    and no expansion — the shape `bin/lib/envfile.sh`
    already writes. A parser that understood more would invite a file this
    hook cannot predict its own read of.
    """
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        name, sep, value = stripped.partition("=")
        if sep and _ENV_NAME_RE.fullmatch(name):
            values[name] = value

    return values


def _checks(config: Config) -> list[Check]:
    return [
        Check("config", True, f"bind {config.bind}:{config.port}"),
        _readable("families", config.families_dir),
        _readable("audit", config.audit_dir),
        _readable("registry", config.registry_dir),
        _readable("view-ro token", config.view_token_file),
        _healthy(config),
    ]


def _readable(name: str, path: Path) -> Check:
    """Present and openable. A directory is listed, a file is stat'd."""
    if not path.exists():
        return Check(name, False, f"{path} is missing")

    try:
        if path.is_dir():
            next(iter(path.iterdir()), None)
        else:
            path.stat()
    except OSError as error:
        # The audit directory is the one that fails here in practice: the
        # user manager's groups are frozen until the host reboots
        # (README gotcha 4).
        return Check(name, False, f"{path}: {error.strerror or error}")

    return Check(name, True, str(path))


def _healthy(config: Config) -> Check:
    """The one call. `/healthz` is keyless, so no secret is needed."""
    url = f"http://{_host(config.bind)}:{config.port}/healthz"

    try:
        answer = httpx.get(url, timeout=HEALTH_TIMEOUT_S)
    except httpx.HTTPError as error:
        return Check("healthz", False, f"{url}: {type(error).__name__}: {error}")

    if answer.status_code != 200:
        return Check("healthz", False, f"{url} answered {answer.status_code}")

    return Check("healthz", True, url)


def _host(bind: str) -> str:
    """An IPv6 literal needs brackets in a URL. Every other bind does not."""
    return f"[{bind}]" if ":" in bind else bind


def _report(checks: list[Check], *, as_json: bool) -> int:
    failed = [one for one in checks if not one.ok]

    if as_json:
        body = {
            "ok": not failed,
            "checks": [{"name": one.name, "ok": one.ok, "detail": one.detail} for one in checks],
        }
        print(json.dumps(body, indent=2))
    else:
        for one in checks:
            print(f"{'PASS' if one.ok else 'FAIL'} {one.name}: {one.detail}")

    return EXIT_FAILED if failed else EXIT_OK


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="view-verify", description="Does agent-view work right now? (contract 06 §4)"
    )
    parser.add_argument("--json", action="store_true", help="one JSON object instead of lines")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="KEY=value lines to read before checking the environment",
    )

    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
