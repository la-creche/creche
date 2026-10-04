"""Written `component.yaml` inputs: one per rule, per shape error and per YAML trap.

Every case is YAML TEXT, not a dict dumped to YAML. Half of them are about
what PyYAML does with the text before `handover.manifest` sees a value: a
YAML 1.1 scalar, a tag, an anchor, a merge key, a directive. A reader in
another language meets the same text and must give the same answer.

An input on which `parse_manifest` raises is not in this list
(`vectors/AGENTS.md`, rule 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: The site file a case is read with. `SITE_SET` names an operator account
#: and its home. `SITE_MISSING` is a host with no site file.
SITE_SET: Final = "set"
SITE_MISSING: Final = "missing"

#: The operator account of the site file that the cases are read with. It
#: is the name of no deployment.
OPERATOR_USER: Final = "keeper"
OPERATOR_HOME: Final = "/home/keeper"

#: The largest `component.yaml` that `parse_manifest` reads, in bytes.
MAX_BYTES: Final = 16 * 1024

ARABIC_INDIC_ZERO: Final = "\u0660"
ARABIC_INDIC_FOUR: Final = "\u0664"


@dataclass(frozen=True)
class Case:
    """One `component.yaml`: an id and its text, or the parts of a long text."""

    id: str
    text: str = ""
    site: str = SITE_SET
    #: A long input, written as repeated parts in place of `text`.
    parts: tuple[tuple[str, int], ...] = ()


#: One valid manifest, field by field. A case replaces the lines of a field.
FIELDS: Final[dict[str, str]] = {
    "manifest_version": 'manifest_version: "0.6"',
    "name": "name: attendance",
    "repo": "repo: agent-control",
    "path": "path: attendance",
    "kind": "kind: venv",
    "unit": "unit: creche-attendance.service",
    "runs_as": "runs_as: operator",
    "build": (
        "build:\n"
        '  - ["/usr/local/bin/uv", "sync", "--frozen", "--no-editable", "--package", "attendance"]'
    ),
    "install": (
        "install:\n"
        "  to: ~/.local/components/attendance\n"
        "  prev: ~/.local/components/attendance.prev"
    ),
    "provides": "provides:\n  - { contract: session-api, major: 1, minor: 4 }",
    "requires": (
        "requires:\n"
        "  - { contract: channel, major: 1, min_minor: 3 }\n"
        "  - { contract: pep-grant, major: 2, min_minor: 0 }"
    ),
    "depends_on": "depends_on: [chaperone]",
    "verify": (
        "verify:\n"
        '  command: ["~/.local/components/attendance/bin/attendance-verify", "--json"]\n'
        "  user: operator\n"
        "  timeout_s: 60"
    ),
    "restore": "restore:\n  mode: automatic\n  keep: 1",
    "secrets": "secrets: [session_key]",
    "release": "release: yes",
}


def manifest(**replace: str | None) -> str:
    """The valid manifest with the lines of some fields replaced. `None` drops a field."""
    unknown = replace.keys() - FIELDS.keys()
    if unknown:
        raise ValueError(f"no such field: {sorted(unknown)}")

    lines = [replace.get(name, line) for name, line in FIELDS.items()]

    return "\n".join(line for line in lines if line is not None) + "\n"


FULL: Final = manifest()


def _with(case_id: str, **replace: str | None) -> Case:
    return Case(case_id, manifest(**replace))


def _no_site(case_id: str, **replace: str | None) -> Case:
    """A case for a host with no site file."""
    return Case(case_id, manifest(**replace), SITE_MISSING)


def _plus(case_id: str, extra: str) -> Case:
    """The valid manifest and more text after it."""
    return Case(case_id, FULL + extra)


def _install(to: str, prev: str) -> str:
    return f"install:\n  to: {to}\n  prev: {prev}"


def _verify(command: str, user: str = "operator", timeout: str = "60") -> str:
    return f"verify:\n  command: {command}\n  user: {user}\n  timeout_s: {timeout}"


def _restore(mode: str, keep: str) -> str:
    return f"restore:\n  mode: {mode}\n  keep: {keep}"


def _words(count: int) -> str:
    """One argv of `count` words. The first word is an absolute path."""
    return "[" + ", ".join(["/bin/tool", *["w"] * (count - 1)]) + "]"


def _padded(size: int) -> tuple[tuple[str, int], ...]:
    """The valid manifest and one comment line, `size` bytes in all."""
    return ((FULL, 1), ("#", size - len(FULL.encode("utf-8")) - 1), ("\n", 1))


_DATA_FIELDS: Final = {
    "name": "name: registry-data",
    "repo": "repo: agent-registry",
    "path": "path: .",
    "kind": "kind: data",
    "unit": "unit: null",
    "runs_as": "runs_as: none",
    "build": None,
    "install": _install("/srv/agents/registry", "/srv/agents/registry.prev"),
    "provides": None,
    "requires": "requires:\n  - { contract: family-file, major: 1, min_minor: 0 }",
    "depends_on": None,
    "verify": _verify('["/usr/bin/true"]', "root", "1"),
    "secrets": None,
    "release": "release: no",
}

_BINARY_FIELDS: Final = {
    "name": "name: chaperone",
    "path": "path: chaperone",
    "kind": "kind: binary",
    "unit": "unit: creche-chaperone.service",
    "runs_as": "runs_as: root",
    "build": (
        "build:\n"
        '  - ["/usr/local/bin/cargo", "build", "--release", "--locked", "--bin", "chaperone"]'
    ),
    "install": _install("/opt/components/chaperone", "/opt/components/chaperone.prev"),
    "provides": "provides:\n  - { contract: pep-grant, major: 2, minor: 1 }",
    "requires": "requires: []",
    "depends_on": "depends_on: []",
    "verify": _verify('["/opt/components/chaperone/bin/chaperone-verify"]', "root", "300"),
    "secrets": "secrets: []",
}


def _merge_chain(levels: int) -> dict[str, str]:
    """A `restore` that merges the last mapping of a chain of `levels` merge keys.

    The mappings of the chain are in a first value of `unit`, which a second
    `unit` line replaces. PyYAML reads `restore` before it reads a mapping of
    the chain, so the merge of `restore` walks the whole chain.
    """
    chain = "".join(f"\n  - &m{n} {{<<: *m{n - 1}}}" for n in range(1, levels))

    return {
        "unit": "unit:\n  - &m0 {mode: automatic}" + chain + "\n" + FIELDS["unit"],
        "restore": f"restore:\n  <<: *m{levels - 1}\n  keep: 1",
    }


def _merge_copies(keys: int, merges: int) -> str:
    """A `unit` whose first value merges one mapping of `keys` keys `merges` times.

    A second `unit` line replaces the value. PyYAML does the merge first.
    """
    pairs = ", ".join(f"k{n}: 1" for n in range(keys))
    aliases = ", ".join(["*a"] * merges)

    return f"unit:\n  - &a {{{pairs}}}\n  - {{<<: [{aliases}]}}\n" + FIELDS["unit"]


_OPTIONAL: Final = ("build", "provides", "requires", "depends_on", "secrets")
_REQUIRED: Final = tuple(name for name in FIELDS if name not in _OPTIONAL)

_PATH_256: Final = "/".join(["s" * 64, *["s" * 63] * 3])
_PATH_257: Final = "/".join(["s" * 64, "s" * 64, "s" * 63, "s" * 63])

CASES: Final[tuple[Case, ...]] = (
    # --- accepted: one per kind (\u00a78) -----------------------------------------
    Case("full", FULL),
    _with("kind-binary", **_BINARY_FIELDS),
    _with("kind-oci-image", kind="kind: oci-image", runs_as="runs_as: sandbox"),
    _with("kind-compose", kind="kind: compose", runs_as="runs_as: root"),
    _with("kind-data", **_DATA_FIELDS),
    _with("kind-venv-mcp", kind="kind: venv", runs_as="runs_as: mcp", path="path: ."),
    _with("only-required-fields", **dict.fromkeys(_OPTIONAL)),
    _with(
        "optional-fields-null",
        build="build: ~",
        provides="provides:",
        requires="requires: null",
        depends_on="depends_on: Null",
        secrets="secrets: NULL",
    ),
    _with(
        "optional-fields-empty",
        build="build: []",
        provides="provides: []",
        requires="requires: []",
        depends_on="depends_on: []",
        secrets="secrets: []",
    ),
    # --- the top level (\u00a710) ------------------------------------------------
    Case("top-empty", ""),
    Case("top-comment-only", "# nothing\n"),
    Case("top-null", "~\n"),
    Case("top-list", "- name: attendance\n"),
    Case("top-text", "attendance\n"),
    Case("top-number", "6\n"),
    Case("top-empty-mapping", "{}\n"),
    Case("top-key-number", "1: a\n" + FULL),
    Case("top-key-bool", "yes: a\n" + FULL),
    Case("top-key-null", "~: a\n" + FULL),
    Case("top-key-float", "1.5: a\n" + FULL),
    Case("top-key-date", "2026-10-03: a\n" + FULL),
    Case("top-key-list", "? [a, b]\n: c\n" + FULL),
    Case("top-key-mapping", "? {a: b}\n: c\n" + FULL),
    _plus("unknown-field", "owner: nobody\n"),
    _plus("unknown-fields-sorted", "zeta: 1\nalpha: 2\n"),
    _plus("unknown-field-unsafe-name", '"not a token": 1\n'),
    _plus("unknown-field-65-chars", "k" * 65 + ": 1\n"),
    _plus("unknown-field-64-chars", "k" * 64 + ": 1\n"),
    _plus("unknown-field-output", "output: UV_PROJECT_ENVIRONMENT\n"),
    _plus("unknown-field-unit-file", "unit_file: systemd/creche-attendance.service\n"),
    *(_with(f"missing-{name}", **{name: None}) for name in _REQUIRED),
    _with("missing-two-sorted", verify=None, install=None),
    _with("unknown-before-missing", name=None, kind="kind: venv\nowner: nobody"),
    _plus("duplicate-key-last-wins", "name: caregiver\n"),
    _plus("duplicate-key-bad-then-good", "kind: wheel\nkind: venv\n"),
    Case("size-at-cap", parts=_padded(MAX_BYTES)),
    Case("size-over-cap", parts=_padded(MAX_BYTES + 1)),
    Case("size-over-cap-not-yaml", parts=(("[", MAX_BYTES + 1),)),
    Case("size-two-byte-chars-over-cap", parts=((FULL, 1), ("# ", 1), ("\u00e9", MAX_BYTES // 2))),
    # --- manifest_version ---------------------------------------------------
    _with("version-zero-zero", manifest_version='manifest_version: "0.0"'),
    _with("version-above-minor", manifest_version='manifest_version: "0.7"'),
    _with("version-major-one", manifest_version='manifest_version: "1.0"'),
    _with("version-leading-zero", manifest_version='manifest_version: "0.06"'),
    _with("version-float", manifest_version="manifest_version: 0.6"),
    _with("version-single-quoted", manifest_version="manifest_version: '0.4'"),
    _with("version-one-number", manifest_version='manifest_version: "0"'),
    _with("version-three-numbers", manifest_version='manifest_version: "0.6.1"'),
    _with("version-final-newline", manifest_version='manifest_version: "0.6\\n"'),
    _with("version-space", manifest_version='manifest_version: "0. 6"'),
    _with("version-empty", manifest_version='manifest_version: ""'),
    _with("version-null", manifest_version="manifest_version:"),
    _with("version-513-chars", manifest_version=f'manifest_version: "{"0" * 512}.6"'),
    _with("version-512-chars", manifest_version=f'manifest_version: "0.{"0" * 509}6"'),
    _with(
        "version-arabic-indic",
        manifest_version=f'manifest_version: "{ARABIC_INDIC_ZERO}.{ARABIC_INDIC_FOUR}"',
    ),
    # --- name ---------------------------------------------------------------
    _with("name-two-chars", name="name: ab"),
    _with("name-one-char", name="name: a"),
    _with("name-31-chars", name="name: " + "a" * 31),
    _with("name-32-chars", name="name: " + "a" * 32),
    _with("name-upper", name="name: Attendance"),
    _with("name-underscore", name="name: atten_dance"),
    _with("name-leading-digit", name="name: 1attendance"),
    _with("name-final-newline", name='name: "attendance\\n"'),
    _with("name-not-in-catalog", name="name: nobody"),
    _with("name-number", name="name: 5"),
    _with("name-null", name="name:"),
    _with("name-list", name="name: [attendance]"),
    _with("name-mapping", name="name: {a: b}"),
    _with("name-bool-word", name="name: on"),
    _with("name-null-word", name="name: null"),
    _with("name-quoted-null-word", name='name: "null"'),
    # --- repo, kind (closed sets) -------------------------------------------
    _with("repo-agent-mcp", repo="repo: agent-mcp"),
    _with("repo-agent-registry", repo="repo: agent-registry"),
    _with("repo-other", repo="repo: agent-other"),
    _with("repo-upper", repo="repo: Agent-Control"),
    _with("repo-unsafe", repo='repo: "not a repo"'),
    _with("repo-bool", repo="repo: yes"),
    _with("repo-number", repo="repo: 7"),
    _with("repo-65-chars", repo="repo: " + "r" * 65),
    _with("repo-final-newline", repo='repo: "agent-control\\n"'),
    _with("kind-other", kind="kind: wheel"),
    _with("kind-upper", kind="kind: Venv"),
    _with("kind-null", kind="kind: ~"),
    _with("kind-list", kind="kind: [venv]"),
    # --- path ---------------------------------------------------------------
    _with("path-dot", path="path: ."),
    _with("path-nested", path="path: a/b.c/d_e-f"),
    _with("path-absolute", path="path: /attendance"),
    _with("path-backslash", path='path: "a\\\\b"'),
    _with("path-nul", path='path: "a\\0b"'),
    _with("path-empty-segment", path="path: a//b"),
    _with("path-parent", path="path: a/../b"),
    _with("path-dot-segment", path="path: a/./b"),
    _with("path-dot-first", path="path: ./a"),
    _with("path-final-slash", path="path: a/"),
    _with("path-space", path='path: "a b"'),
    _with("path-not-ascii", path="path: caf\u00e9"),
    _with("path-empty", path='path: ""'),
    _with("path-256-chars", path="path: " + _PATH_256),
    _with("path-257-chars", path="path: " + _PATH_257),
    _with("path-segment-64-chars", path="path: " + "s" * 64),
    _with("path-segment-65-chars", path="path: " + "s" * 65),
    _with("path-513-chars", path="path: " + "s" * 513),
    _with("path-number", path="path: 5"),
    _with("path-tilde", path='path: "~/attendance"'),
    # --- unit ---------------------------------------------------------------
    _with("unit-null-word", unit="unit: null"),
    _with("unit-tilde", unit="unit: ~"),
    _with("unit-empty", unit="unit:"),
    _with("unit-template", unit="unit: creche-trigger@.service"),
    _with("unit-64-chars", unit="unit: " + "u" * 64),
    _with("unit-65-chars", unit="unit: " + "u" * 65),
    _with("unit-space", unit='unit: "bad unit"'),
    _with("unit-slash", unit="unit: a/b.service"),
    _with("unit-final-newline", unit='unit: "x.service\\n"'),
    _with("unit-empty-text", unit='unit: ""'),
    _with("unit-number", unit="unit: 5"),
    _with("unit-false", unit="unit: false"),
    # --- runs_as and verify.user (the operator account) ---------------------
    _with("runs-as-root", runs_as="runs_as: root"),
    _with("runs-as-sandbox", runs_as="runs_as: sandbox"),
    _with("runs-as-mcp", runs_as="runs_as: mcp"),
    _with("runs-as-none", runs_as="runs_as: none"),
    _with("runs-as-account-name", runs_as=f"runs_as: {OPERATOR_USER}"),
    _no_site("runs-as-account-name-no-site", runs_as=f"runs_as: {OPERATOR_USER}"),
    _with("runs-as-other-account", runs_as="runs_as: somebody"),
    _with("runs-as-upper", runs_as="runs_as: Root"),
    _with("runs-as-none-word", runs_as="runs_as: None"),
    _with("runs-as-bool", runs_as="runs_as: no"),
    _with("runs-as-null", runs_as="runs_as: ~"),
    _with("runs-as-unsafe", runs_as='runs_as: "a b"'),
    _with("verify-user-root", verify=_verify('["/bin/true"]', "root")),
    _with("verify-user-account-name", verify=_verify('["/bin/true"]', OPERATOR_USER)),
    _with("verify-user-sandbox", verify=_verify('["/bin/true"]', "sandbox")),
    _with("verify-user-number", verify=_verify('["/bin/true"]', "0")),
    # --- build --------------------------------------------------------------
    _with("build-two-commands", build='build:\n  - ["/bin/a", "x"]\n  - ["/bin/b"]'),
    _with("build-block-argv", build="build:\n  -\n    - /bin/a\n    - x"),
    _with("build-home-argv0", build='build:\n  - ["~/bin/a", "~/not-expanded"]'),
    _no_site("build-home-argv0-no-site", build='build:\n  - ["~/bin/a"]'),
    _with("build-other-home", build='build:\n  - ["~keeper/bin/a"]'),
    _with("build-relative-argv0", build='build:\n  - ["uv", "sync"]'),
    _with("build-text", build='build: "/bin/a x"'),
    _with("build-argv-text", build='build:\n  - "/bin/a x"'),
    _with("build-argv-mapping", build="build:\n  - { cmd: /bin/a }"),
    _with("build-argv-empty", build="build:\n  - []"),
    _with("build-argv-32-words", build="build:\n  - " + _words(32)),
    _with("build-argv-33-words", build="build:\n  - " + _words(33)),
    _with("build-word-number", build='build:\n  - ["/bin/a", 5]'),
    _with("build-word-bool", build='build:\n  - ["/bin/a", yes]'),
    _with("build-word-null", build='build:\n  - ["/bin/a", ~]'),
    _with("build-word-list", build='build:\n  - ["/bin/a", [x]]'),
    _with("build-word-empty", build='build:\n  - ["/bin/a", ""]'),
    _with("build-word-512-chars", build=f'build:\n  - ["/bin/a", "{"w" * 512}"]'),
    _with("build-word-513-chars", build=f'build:\n  - ["/bin/a", "{"w" * 513}"]'),
    _with("build-word-512-two-byte-chars", build=f'build:\n  - ["/bin/a", "{"\u00e9" * 512}"]'),
    _with("build-word-newline", build='build:\n  - ["/bin/a", "x\\ny"]'),
    _with("build-word-nul", build='build:\n  - ["/bin/a", "x\\0y"]'),
    _with("build-word-carriage-return", build='build:\n  - ["/bin/a", "x\\ry"]'),
    _with("build-word-lone-surrogate", build='build:\n  - ["/bin/a", "\\ud800"]'),
    _with("build-word-option-like", build='build:\n  - ["/bin/a", "--flag=$(x)", "; rm"]'),
    _with("build-argv0-parent", build='build:\n  - ["/usr/../bin/a"]'),
    _with("build-argv0-double-slash", build='build:\n  - ["/usr//bin/a"]'),
    _with("build-argv0-two-leading-slashes", build='build:\n  - ["//usr/bin/a"]'),
    _with("build-argv0-three-leading-slashes", build='build:\n  - ["///usr/bin/a"]'),
    _with("build-argv0-dot-segment", build='build:\n  - ["/usr/./bin/a"]'),
    _with("build-argv0-final-slash", build='build:\n  - ["/usr/bin/"]'),
    _with("build-argv0-root", build='build:\n  - ["/"]'),
    _with("build-argv0-nul", build='build:\n  - ["/usr/b\\0in"]'),
    _with("build-argv0-257-chars", build=f'build:\n  - ["/{"p" * 256}"]'),
    _with("build-argv0-256-chars", build=f'build:\n  - ["/{"p" * 255}"]'),
    _with("build-64-commands", build="build:\n" + "\n".join(['  - ["/bin/a"]'] * 64)),
    _with("build-65-commands", build="build:\n" + "\n".join(['  - ["/bin/a"]'] * 65)),
    _with("build-mapping", build="build:\n  cmd: /bin/a"),
    _with("build-number", build="build: 5"),
    _with("build-false", build="build: false"),
    # --- install ------------------------------------------------------------
    _with("install-absolute", install=_install("/opt/components/x", "/opt/components/x.prev")),
    _no_site("install-home-no-site"),
    _with("install-other-home", install=_install("~keeper/x", "/opt/x.prev")),
    _with("install-tilde-alone", install=_install('"~"', "/opt/x.prev")),
    _with("install-relative", install=_install("opt/x", "/opt/x.prev")),
    _with("install-prev-relative", install=_install("/opt/x", "x.prev")),
    _with("install-same", install=_install("/opt/x", "/opt/x")),
    _with("install-same-through-home", install=_install("~/x", f"{OPERATOR_HOME}/x")),
    _with("install-parent", install=_install("/opt/components/../../etc", "/opt/x.prev")),
    _with("install-final-slash", install=_install("/opt/x/", "/opt/x.prev")),
    _with("install-not-normalized", install=_install("/opt//x", "/opt/x.prev")),
    _with("install-two-leading-slashes", install=_install("//opt/x", "/opt/x.prev")),
    _with("install-257-chars", install=_install("/" + "p" * 256, "/opt/x.prev")),
    _with("install-home-257-chars", install=_install("~/" + "p" * 244, "/opt/x.prev")),
    _with("install-empty", install=_install('""', "/opt/x.prev")),
    _with("install-number", install=_install("5", "/opt/x.prev")),
    _with("install-no-to", install="install:\n  prev: /opt/x.prev"),
    _with("install-no-prev", install="install:\n  to: /opt/x"),
    _with("install-unknown-key", install=_install("/opt/x", "/opt/x.prev") + "\n  owner: root"),
    _with("install-key-number", install=_install("/opt/x", "/opt/x.prev") + "\n  1: root"),
    _with("install-list", install="install: [/opt/x, /opt/x.prev]"),
    _with("install-text", install="install: /opt/x"),
    _with("install-null", install="install:"),
    _with("install-not-ascii", install=_install("/opt/caf\u00e9", "/opt/x.prev")),
    _with("install-space", install=_install('"/opt/a b"', "/opt/x.prev")),
    _with("install-lone-surrogate", install=_install('"/opt/\\ud800"', "/opt/x.prev")),
    _with("install-final-newline", install=_install('"/opt/x\\n"', "/opt/x.prev")),
    _with("install-control-characters", install=_install('"/opt/x\\ty\\x1b"', "/opt/x.prev")),
    # --- provides and requires (\u00a73.1) ---------------------------------------
    _with(
        "provides-two",
        provides="provides:\n  - {contract: channel, major: 0, minor: 0}\n"
        "  - {contract: family-file, major: 999, minor: 999}",
    ),
    _with(
        "provides-same-twice",
        provides="provides:\n  - {contract: channel, major: 1, minor: 1}\n"
        "  - {contract: channel, major: 1, minor: 1}",
    ),
    _with(
        "provides-block",
        provides="provides:\n  - contract: manager-status\n    major: 1\n    minor: 2",
    ),
    _with(
        "provides-other-contract", provides="provides:\n  - {contract: other, major: 1, minor: 1}"
    ),
    _with("provides-contract-number", provides="provides:\n  - {contract: 1, major: 1, minor: 1}"),
    _with(
        "provides-major-1000", provides="provides:\n  - {contract: channel, major: 1000, minor: 1}"
    ),
    _with(
        "provides-major-negative",
        provides="provides:\n  - {contract: channel, major: -1, minor: 1}",
    ),
    _with(
        "provides-major-text", provides='provides:\n  - {contract: channel, major: "1", minor: 1}'
    ),
    _with(
        "provides-major-bool", provides="provides:\n  - {contract: channel, major: true, minor: 1}"
    ),
    _with(
        "provides-major-float", provides="provides:\n  - {contract: channel, major: 1.0, minor: 1}"
    ),
    _with(
        "provides-major-octal", provides="provides:\n  - {contract: channel, major: 010, minor: 1}"
    ),
    _with(
        "provides-major-hex", provides="provides:\n  - {contract: channel, major: 0x10, minor: 1}"
    ),
    _with(
        "provides-major-binary",
        provides="provides:\n  - {contract: channel, major: 0b11, minor: 1}",
    ),
    _with(
        "provides-major-sexagesimal",
        provides="provides:\n  - {contract: channel, major: 1:30, minor: 1}",
    ),
    _with(
        "provides-major-underscore",
        provides="provides:\n  - {contract: channel, major: 1_0, minor: 1}",
    ),
    _with(
        "provides-major-plus", provides="provides:\n  - {contract: channel, major: +7, minor: 1}"
    ),
    _with(
        "provides-major-new-octal",
        provides="provides:\n  - {contract: channel, major: 0o10, minor: 1}",
    ),
    _with(
        "provides-major-past-64-bits",
        provides="provides:\n  - {contract: channel, major: " + "9" * 30 + ", minor: 1}",
    ),
    _with(
        "provides-major-negative-past-64-bits",
        provides="provides:\n  - {contract: channel, major: -" + "9" * 30 + ", minor: 1}",
    ),
    _with(
        "provides-minor-1000", provides="provides:\n  - {contract: channel, major: 1, minor: 1000}"
    ),
    _with("provides-no-minor", provides="provides:\n  - {contract: channel, major: 1}"),
    _with("provides-no-contract", provides="provides:\n  - {major: 1, minor: 1}"),
    _with(
        "provides-min-minor", provides="provides:\n  - {contract: channel, major: 1, min_minor: 1}"
    ),
    _with(
        "provides-unknown-key",
        provides="provides:\n  - {contract: channel, major: 1, minor: 1, note: x}",
    ),
    _with(
        "provides-key-number",
        provides="provides:\n  - {contract: channel, major: 1, minor: 1, 2: x}",
    ),
    _with("provides-entry-text", provides="provides:\n  - channel"),
    _with("provides-entry-list", provides="provides:\n  - [channel, 1, 1]"),
    _with("provides-entry-null", provides="provides:\n  -"),
    _with("provides-mapping", provides="provides: {contract: channel, major: 1, minor: 1}"),
    _with("provides-text", provides="provides: channel"),
    _with(
        "provides-64",
        provides="provides:\n" + "\n".join(["  - {contract: channel, major: 1, minor: 1}"] * 64),
    ),
    _with(
        "provides-65",
        provides="provides:\n" + "\n".join(["  - {contract: channel, major: 1, minor: 1}"] * 65),
    ),
    _with("requires-minor", requires="requires:\n  - {contract: channel, major: 1, minor: 1}"),
    _with(
        "requires-other-contract",
        requires="requires:\n  - {contract: Channel, major: 1, min_minor: 1}",
    ),
    _with(
        "requires-min-minor-1000",
        requires="requires:\n  - {contract: channel, major: 1, min_minor: 1000}",
    ),
    _with("requires-entry-text", requires="requires:\n  - channel"),
    _with(
        "requires-bad-and-version-bad",
        requires="requires:\n  - channel",
        manifest_version='manifest_version: "9.9"',
    ),
    _with(
        "provides-bad-and-requires-bad",
        provides="provides:\n  - channel",
        requires="requires:\n  - {contract: nothing}",
    ),
    # --- depends_on and secrets ---------------------------------------------
    _with("depends-two", depends_on="depends_on: [chaperone, caregiver]"),
    _with("depends-block", depends_on="depends_on:\n  - chaperone\n  - caregiver"),
    _with("depends-not-in-catalog", depends_on="depends_on: [nobody]"),
    _with("depends-self", depends_on="depends_on: [attendance]"),
    _with("depends-repeat", depends_on="depends_on: [chaperone, caregiver, chaperone]"),
    _with("depends-upper", depends_on="depends_on: [Chaperone]"),
    _with("depends-one-char", depends_on="depends_on: [c]"),
    _with("depends-number", depends_on="depends_on: [5]"),
    _with("depends-null-entry", depends_on="depends_on: [~]"),
    _with("depends-text", depends_on="depends_on: chaperone"),
    _with("depends-mapping", depends_on="depends_on: {chaperone: yes}"),
    _with("depends-64", depends_on="depends_on: [" + ", ".join(f"c-{n}" for n in range(64)) + "]"),
    _with("depends-65", depends_on="depends_on: [" + ", ".join(f"c-{n}" for n in range(65)) + "]"),
    _with("secrets-two", secrets="secrets: [session_key, db_password]"),
    _with("secrets-same-twice", secrets="secrets: [session_key, session_key]"),
    _with("secrets-hyphen", secrets="secrets: [session-key]"),
    _with("secrets-upper", secrets="secrets: [SESSION_KEY]"),
    _with("secrets-one-char", secrets="secrets: [s]"),
    _with("secrets-63-chars", secrets="secrets: [" + "s" * 63 + "]"),
    _with("secrets-64-chars", secrets="secrets: [" + "s" * 64 + "]"),
    _with("secrets-value-like", secrets='secrets: ["hunter2 "]'),
    _with("secrets-number", secrets="secrets: [5]"),
    _with("secrets-mapping-entry", secrets="secrets: [{name: session_key}]"),
    _with("secrets-text", secrets="secrets: session_key"),
    _with("secrets-65", secrets="secrets: [" + ", ".join(f"s_{n}" for n in range(65)) + "]"),
    # --- verify (\u00a74) --------------------------------------------------------
    _with("verify-timeout-1", verify=_verify('["/bin/true"]', timeout="1")),
    _with("verify-timeout-300", verify=_verify('["/bin/true"]', timeout="300")),
    _with("verify-timeout-0", verify=_verify('["/bin/true"]', timeout="0")),
    _with("verify-timeout-301", verify=_verify('["/bin/true"]', timeout="301")),
    _with("verify-timeout-negative", verify=_verify('["/bin/true"]', timeout="-1")),
    _with("verify-timeout-text", verify=_verify('["/bin/true"]', timeout='"60"')),
    _with("verify-timeout-float", verify=_verify('["/bin/true"]', timeout="60.0")),
    _with("verify-timeout-bool", verify=_verify('["/bin/true"]', timeout="true")),
    _with("verify-timeout-null", verify=_verify('["/bin/true"]', timeout="~")),
    _with("verify-timeout-sexagesimal", verify=_verify('["/bin/true"]', timeout="1:00")),
    _with("verify-timeout-octal", verify=_verify('["/bin/true"]', timeout="0100")),
    _with("verify-command-text", verify=_verify('"/bin/true --json"')),
    _with("verify-command-list-of-lists", verify=_verify('[["/bin/true"]]')),
    _with("verify-command-empty", verify=_verify("[]")),
    _with("verify-command-relative", verify=_verify('["true"]')),
    _with("verify-command-33-words", verify=_verify(_words(33))),
    _no_site("verify-command-home-no-site", install=_install("/opt/x", "/opt/x.prev")),
    _with("verify-command-null", verify="verify:\n  command:\n  user: root\n  timeout_s: 60"),
    _with("verify-no-command", verify="verify:\n  user: root\n  timeout_s: 60"),
    _with("verify-no-user", verify='verify:\n  command: ["/bin/true"]\n  timeout_s: 60'),
    _with("verify-no-timeout", verify='verify:\n  command: ["/bin/true"]\n  user: root'),
    _with("verify-unknown-key", verify=_verify('["/bin/true"]') + "\n  shell: true"),
    _with("verify-list", verify="verify: [/bin/true]"),
    _with("verify-null", verify="verify: ~"),
    # --- restore (\u00a75) -------------------------------------------------------
    _with("restore-manual", restore=_restore("manual", "1")),
    _with("restore-keep-10", restore=_restore("automatic", "10")),
    _with("restore-keep-3", restore=_restore("automatic", "3")),
    _with("restore-keep-0", restore=_restore("automatic", "0")),
    _with("restore-keep-11", restore=_restore("automatic", "11")),
    _with("restore-keep-text", restore=_restore("automatic", '"1"')),
    _with("restore-mode-other", restore=_restore("never", "1")),
    _with("restore-mode-upper", restore=_restore("Automatic", "1")),
    _with("restore-mode-bool", restore=_restore("off", "1")),
    _with("restore-no-mode", restore="restore:\n  keep: 1"),
    _with("restore-no-keep", restore="restore:\n  mode: automatic"),
    _with("restore-unknown-key", restore=_restore("automatic", "1") + "\n  group: all"),
    _with("restore-text", restore="restore: automatic"),
    # --- release ------------------------------------------------------------
    _with("release-no", release="release: no"),
    _with("release-true", release="release: true"),
    _with("release-false", release="release: false"),
    _with("release-on", release="release: on"),
    _with("release-off", release="release: Off"),
    _with("release-upper-yes", release="release: YES"),
    _with("release-quoted-yes", release='release: "yes"'),
    _with("release-quoted-no", release="release: 'no'"),
    _with("release-quoted-true", release='release: "true"'),
    _with("release-quoted-upper", release='release: "Yes"'),
    _with("release-y", release="release: y"),
    _with("release-other", release="release: maybe"),
    _with("release-number", release="release: 1"),
    _with("release-null", release="release: ~"),
    _with("release-list", release="release: [yes]"),
    # --- the order of the checks --------------------------------------------
    _with("order-version-before-name", manifest_version='manifest_version: "9.9"', name="name: A"),
    _with("order-name-before-repo", name="name: A", repo="repo: other"),
    _with("order-path-before-kind", path="path: /x", kind="kind: wheel"),
    _with("order-unit-before-runs-as", unit='unit: "a b"', runs_as="runs_as: nobody"),
    _with("order-build-before-install", build="build: 5", install="install: 5"),
    _with("order-install-before-depends", install="install: 5", depends_on="depends_on: 5"),
    _with("order-depends-before-verify", depends_on="depends_on: 5", verify="verify: 5"),
    _with("order-verify-before-restore", verify="verify: 5", restore="restore: 5"),
    _with("order-restore-before-secrets", restore="restore: 5", secrets="secrets: 5"),
    _with("order-secrets-before-release", secrets="secrets: 5", release="release: 5"),
    # --- YAML 1.1 against YAML 1.2 ------------------------------------------
    _with("yaml-yes-text-field", unit="unit: yes"),
    _with("yaml-no-text-field", name="name: no"),
    _with("yaml-true-forms", release="release: True"),
    _with("yaml-false-upper", release="release: FALSE"),
    _with("yaml-n-single", release="release: n"),
    _with("yaml-tilde-text-field", name="name: ~"),
    _with("yaml-timestamp", unit="unit: 2026-10-03"),
    _with("yaml-timestamp-quoted", unit='unit: "2026-10-03"'),
    _with("yaml-datetime", unit="unit: 2026-10-03 12:30:00"),
    _with("yaml-float-text-field", unit="unit: 1.0"),
    _with("yaml-exp-no-sign", restore=_restore("automatic", "1e0")),
    _with("yaml-inf", restore=_restore("automatic", ".inf")),
    _with("yaml-nan", restore=_restore("automatic", ".nan")),
    _with("yaml-hex-keep", restore=_restore("automatic", "0xA")),
    _with("yaml-octal-keep", restore=_restore("automatic", "012")),
    _with("yaml-new-octal-keep", restore=_restore("automatic", "0o12")),
    _with("yaml-binary-keep", restore=_restore("automatic", "0b11")),
    _with("yaml-underscore-keep", restore=_restore("automatic", "1_0")),
    _with("yaml-zero-underscore-keep", restore=_restore("automatic", "0_")),
    _with("yaml-sexagesimal-keep", restore=_restore("automatic", "0:9")),
    _with("yaml-plus-keep", restore=_restore("automatic", "+5")),
    _with("yaml-minus-zero-keep", restore=_restore("automatic", "-0")),
    _with("yaml-leading-zero-nine", restore=_restore("automatic", "09")),
    _with("yaml-merge-key", restore="restore:\n  <<: {mode: automatic}\n  keep: 2"),
    _with(
        "yaml-merge-anchor",
        install="install: &paths\n  to: /opt/x\n  prev: /opt/x.prev",
        restore="restore:\n  <<: *paths\n  mode: automatic\n  keep: 1",
    ),
    _with(
        "yaml-merge-list",
        restore="restore:\n  <<: [{mode: manual}, {mode: automatic, keep: 4}]",
    ),
    _with(
        "yaml-merge-own-key-wins",
        restore="restore:\n  mode: manual\n  keep: 1\n  <<: {mode: automatic}",
    ),
    _with("yaml-merge-text", restore="restore:\n  <<: automatic\n  keep: 1"),
    _with("yaml-merge-list-of-text", restore="restore:\n  <<: [automatic]\n  keep: 1"),
    _with("yaml-merge-as-value", unit="unit: <<"),
    _with("yaml-merge-chain-128", **_merge_chain(128)),
    _with("yaml-merge-chain-200", **_merge_chain(200)),
    _with("yaml-merge-copies-65536", unit=_merge_copies(256, 256)),
    _with("yaml-merge-copies-65792", unit=_merge_copies(256, 257)),
    _with("yaml-value-key", restore="restore:\n  =: x\n  mode: automatic\n  keep: 1"),
    _with("yaml-value-as-value", unit="unit: ="),
    _with(
        "yaml-anchor-alias", depends_on="depends_on: &names [chaperone]", secrets="secrets: *names"
    ),
    _with("yaml-anchor-scalar", name="name: &n attendance", path="path: *n"),
    _with("yaml-alias-undefined", path="path: *nothing"),
    _with("yaml-anchor-twice", name="name: &n attendance", path="path: &n attendance"),
    _with("yaml-alias-recursive", depends_on="depends_on: &loop [*loop]"),
    _with("yaml-alias-recursive-mapping", install="install: &loop\n  to: /opt/x\n  prev: *loop"),
    _with("yaml-tag-str-number", unit="unit: !!str 5"),
    _with("yaml-tag-str-bool", name="name: !!str no"),
    _with("yaml-tag-int", restore=_restore("automatic", '!!int "3"')),
    _with("yaml-tag-int-padded", restore=_restore("automatic", '!!int " 3 "')),
    _with("yaml-tag-int-new-octal", restore=_restore("automatic", "!!int 0o3")),
    _with(
        "yaml-tag-int-arabic-indic", restore=_restore("automatic", f'!!int "{ARABIC_INDIC_FOUR}"')
    ),
    _with("yaml-tag-int-no-break-space", restore=_restore("automatic", '!!int "4\u00a0"')),
    _with("yaml-tag-bool", release='release: !!bool "yes"'),
    _with("yaml-tag-bool-mixed-case", release='release: !!bool "oN"'),
    _with("yaml-tag-null", unit="unit: !!null anything"),
    _with("yaml-tag-float", restore=_restore("automatic", "!!float 1")),
    _with("yaml-tag-non-specific", release='release: ! "yes"'),
    _with("yaml-tag-non-specific-plain", release="release: ! yes"),
    _with("yaml-tag-python", unit="unit: !!python/name:os.system"),
    _with("yaml-tag-python-object", unit="unit: !!python/object/apply:os.system [id]"),
    _with("yaml-tag-local", unit="unit: !secret x.service"),
    _with("yaml-tag-verbatim", unit="unit: !<tag:yaml.org,2002:str> x.service"),
    _with("yaml-tag-verbatim-unknown", unit="unit: !<tag:example.org,2026:x> x.service"),
    _with("yaml-tag-binary", unit="unit: !!binary aGVsbG8="),
    _with("yaml-tag-binary-bad-padding", unit="unit: !!binary aGVsbG8"),
    _with("yaml-tag-binary-not-ascii", unit="unit: !!binary caf\u00e9"),
    _with("yaml-tag-set", depends_on="depends_on: !!set {chaperone}"),
    _with("yaml-tag-omap", depends_on="depends_on: !!omap [{chaperone: 1}]"),
    _with("yaml-tag-omap-not-list", depends_on="depends_on: !!omap {chaperone: 1}"),
    _with("yaml-tag-pairs-two-keys", depends_on="depends_on: !!pairs [{a: 1, b: 2}]"),
    _with("yaml-tag-seq-on-list", depends_on="depends_on: !!seq [chaperone]"),
    _with("yaml-tag-seq-on-text", depends_on="depends_on: !!seq chaperone"),
    _with("yaml-tag-map-on-text", install="install: !!map /opt/x"),
    _with("yaml-tag-str-on-list", unit="unit: !!str [a]"),
    _with("yaml-tag-str-on-value-mapping", unit="unit: !!str {=: x.service}"),
    _with("yaml-tag-handle-undefined", unit="unit: !e!x y"),
    Case(
        "yaml-tag-directive", "%TAG !e! tag:yaml.org,2002:\n---\n" + manifest(unit="unit: !e!str 5")
    ),
    Case("yaml-directive-1-1", "%YAML 1.1\n---\n" + FULL),
    Case("yaml-directive-1-2", "%YAML 1.2\n---\n" + FULL),
    Case("yaml-directive-2-0", "%YAML 2.0\n---\n" + FULL),
    Case("yaml-directive-twice", "%YAML 1.1\n%YAML 1.1\n---\n" + FULL),
    Case("yaml-directive-unknown", "%OTHER x y\n---\n" + FULL),
    Case("yaml-directive-no-document", "%YAML 1.1\n" + FULL),
    Case("yaml-crlf", FULL.replace("\n", "\r\n")),
    Case("yaml-cr", FULL.replace("\n", "\r")),
    Case("yaml-nel", FULL.replace("\n", "\u0085")),
    Case("yaml-line-separator", FULL.replace("\n", "\u2028")),
    Case("yaml-bom", "\ufeff" + FULL),
    Case("yaml-bom-inside", FULL + "\ufeff"),
    Case("yaml-nul", FULL + "\x00"),
    Case("yaml-bell", FULL + "# \x07\n"),
    Case("yaml-delete", FULL + "# \x7f\n"),
    Case("yaml-c1-control", FULL + "# \x80\n"),
    Case("yaml-noncharacter", FULL + "# \ufffe\n"),
    Case("yaml-astral", FULL + "# \U0001f600\n"),
    _with("yaml-escape-nul", unit='unit: "a\\0b"'),
    _with("yaml-escape-hex", unit='unit: "\\x78.service"'),
    _with("yaml-escape-unicode", unit='unit: "\\u0078\\U00000079.service"'),
    _with("yaml-escape-unknown", unit='unit: "\\q"'),
    _with("yaml-escape-short-hex", unit='unit: "\\x7"'),
    _with("yaml-escape-line-fold", unit='unit: "x.ser\\\n    vice"'),
    _with("yaml-double-quoted-fold", name='name: "atten\n  dance"'),
    _with("yaml-single-quoted", name="name: 'attendance'"),
    _with("yaml-single-quoted-quote", unit="unit: 'it''s'"),
    _with("yaml-single-quoted-backslash", unit="unit: 'a\\nb'"),
    _with("yaml-quote-unclosed", name='name: "attendance'),
    _with("yaml-literal-block", name="name: |\n  attendance"),
    _with("yaml-literal-block-strip", name="name: |-\n  attendance"),
    _with("yaml-folded-block-strip", name="name: >-\n  atten\n  dance"),
    _with("yaml-folded-block-keep", name="name: >+\n  attendance\n"),
    _with("yaml-block-indent-indicator", name="name: |2-\n    attendance"),
    _with("yaml-block-indent-zero", name="name: |0\n  attendance"),
    _with("yaml-plain-multiline", name="name: atten\n  dance"),
    _with("yaml-plain-colon", unit="unit: a:b.service"),
    _with("yaml-plain-hash", unit="unit: a#b.service"),
    _with("yaml-plain-hash-after-space", unit="unit: x.service #b"),
    _with("yaml-plain-question", unit="unit: ?x"),
    _with("yaml-plain-leading-dash", unit="unit: -x"),
    _with("yaml-plain-at", unit="unit: @x"),
    _with("yaml-plain-backtick", unit="unit: `x`"),
    _with("yaml-plain-percent", unit="unit: %x"),
    _with("yaml-plain-trailing-space", name="name: attendance   "),
    _with("yaml-comment-eol", kind="kind: venv # the python kind"),
    _with("yaml-comment-no-space", kind="kind: venv# not a comment"),
    _with("yaml-value-on-next-line", name="name:\n  attendance"),
    _with("yaml-tab-indent", install="install:\n\tto: /opt/x\n\tprev: /opt/x.prev"),
    _with("yaml-tab-after-colon", name="name:\tattendance"),
    _with("yaml-tab-before-comment", name="name: attendance\t# a tab"),
    _with("yaml-bad-indent", name="name: attendance\n  repo: agent-control"),
    _with("yaml-dedent-sequence", depends_on="depends_on:\n- chaperone\n- caregiver"),
    _with(
        "yaml-flow-mapping-multiline", install="install: {\n  to: /opt/x,\n  prev: /opt/x.prev\n}"
    ),
    _with("yaml-flow-trailing-comma", depends_on="depends_on: [chaperone, ]"),
    _with("yaml-flow-two-commas", depends_on="depends_on: [chaperone, , caregiver]"),
    _with("yaml-flow-unclosed", depends_on="depends_on: [chaperone"),
    _with("yaml-flow-pair-in-list", depends_on="depends_on: [chaperone: 1]"),
    _with("yaml-flow-mapping-no-value", install="install: {to, prev}"),
    _with("yaml-flow-key-no-space", install='install: {"to":/opt/x, "prev":/opt/x.prev}'),
    _with("yaml-flow-plain-colon-no-space", install="install: {to:/opt/x, prev: /opt/x.prev}"),
    _with("yaml-quoted-key", name='"name": attendance'),
    _with("yaml-explicit-key", name="? name\n: attendance"),
    _with("yaml-key-no-value", name="name: attendance\nextra"),
    _with("yaml-key-1024-chars", unit="unit: x.service\n" + "k" * 1024 + ": 1"),
    _with("yaml-key-1025-chars", unit="unit: x.service\n" + "k" * 1025 + ": 1"),
    _with("yaml-colon-in-flow-key", depends_on="depends_on: [a:b]"),
    _with("yaml-mapping-in-sequence-inline", depends_on="depends_on:\n  - a: b"),
    _with("yaml-sequence-in-mapping-value-line", depends_on="depends_on: - chaperone"),
    _with("yaml-value-then-mapping", name="name: attendance: x"),
    Case("yaml-doc-start", "---\n" + FULL),
    Case("yaml-doc-start-inline", "--- " + FULL.replace("\n", "\n    ", 1)),
    Case("yaml-doc-end", FULL + "...\n"),
    Case("yaml-two-documents", FULL + "---\n" + FULL),
    Case("yaml-doc-end-then-start", FULL + "...\n---\n"),
    Case("yaml-empty-second-document", FULL + "---\n"),
    Case("yaml-leading-blank-lines", "\n\n  \n" + FULL),
    Case("yaml-indented-document", "".join(f"  {line}\n" for line in FULL.splitlines())),
    Case(
        "yaml-flow-document",
        "{"
        + ", ".join(
            [
                'manifest_version: "0.6"',
                "name: attendance",
                "repo: agent-control",
                "path: attendance",
                "kind: venv",
                "unit: ~",
                "runs_as: root",
                "install: {to: /opt/x, prev: /opt/x.prev}",
                "verify: {command: [/bin/true], user: root, timeout_s: 5}",
                "restore: {mode: automatic, keep: 1}",
                "release: no",
            ]
        )
        + "}\n",
    ),
    Case("yaml-nested-64", manifest(unit="unit: " + "[" * 64 + "]" * 64)),
    Case("yaml-nested-200", manifest(unit="unit: " + "[" * 200 + "]" * 200)),
    Case(
        "yaml-nested-200-replaced",
        manifest(unit="unit: " + "[" * 200 + "]" * 200 + "\n" + FIELDS["unit"]),
    ),
    Case(
        "yaml-nested-100-block",
        manifest(unit="unit:" + "".join(f"\n{' ' * n}- " for n in range(1, 101)) + "x"),
    ),
    Case("yaml-tag-on-top", "!!map\n" + FULL),
    Case("yaml-tag-set-on-top", "--- !!set\n" + FULL),
    Case("yaml-anchor-on-top", "--- &all\n" + FULL),
)
