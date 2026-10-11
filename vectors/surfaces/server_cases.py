"""Written `server.yaml` inputs: one per rule and per shape error of contract 01b.

Every case is YAML TEXT. `%NAME%` stands for the directory the case is filed
under, so `name` matches its directory unless the case is about that rule.

No case names a deployment.
"""

from __future__ import annotations

from typing import Final

from vectors.surfaces.family_cases import (
    MERGE_CHAIN_MAX,
    MERGED_ENTRIES_MAX,
    Case,
    Repeated,
    merge_chain,
    merged_entries,
)

#: A 64-character lowercase hexadecimal digest.
DIGEST: Final = "0123456789abcdef" * 4

# An `agent-mcp` server every rule accepts. A case appends to it.
HEAD: Final = """\
name: %NAME%
identity: One written server.
install: { source: agent-mcp }
run: { entrypoint: example-mcp }
"""

PYPI: Final = """\
name: %NAME%
identity: One written server.
install:
  source: pypi
  package: example-mcp
  version: 1.0.2
  lock: mcp/%NAME%/install.lock
run: { entrypoint: example-mcp }
"""

RELEASE: Final = f"""\
name: %NAME%
identity: One written server.
install:
  source: github-release
  repo: example-owner/example-mcp
  version: 1.12.0
  asset: example-mcp_Linux_x86_64.tar.gz
  sha256: {DIGEST}
run: {{ entrypoint: example-mcp }}
"""

TOOLS: Final = """\
tools:
  - { name: read_one, description: Read one thing. }
  - { name: write_one, description: Write one thing., write: true }
"""


def _case(case_id: str, text: str, directory: str = "") -> Case:
    return Case(case_id, text.encode("utf-8"), directory)


def _install(**fields: str) -> str:
    """`HEAD` with another `install` block."""
    block = ", ".join(f"{key}: {value}" for key, value in fields.items())

    return HEAD.replace("install: { source: agent-mcp }", f"install: {{ {block} }}")


def _run(block: str) -> str:
    """`HEAD` with another `run` block."""
    return HEAD.replace("run: { entrypoint: example-mcp }", f"run: {block}")


def _pypi(**lines: str) -> str:
    """`PYPI` with whole lines of `install` replaced, by their key."""
    out: list[str] = []
    for line in PYPI.splitlines():
        key = line.strip().partition(":")[0]
        out.append(f"  {key}: {lines[key]}" if key in lines and line.startswith("  ") else line)

    return "\n".join(out) + "\n"


#: A server with the key that the cases about merge keys repeat.
_REPEATED: Final = Repeated(HEAD, "tools", "[]")

CASES: Final[tuple[Case, ...]] = (
    # --- the control: nothing wrong ---
    _case("ok-agent-mcp", HEAD),
    _case("ok-pypi", PYPI),
    _case("ok-release", RELEASE),
    _case(
        "ok-every-field",
        RELEASE
        + """\
tools:
  - { name: read_one, description: Read one thing., write: false }
  - { name: Write-Two_2, description: Write one thing., write: true }
arg_allows:
  - { tools: all, arg: owner, values: [example-owner] }
arg_denies:
  - { tools: [read_one], arg: owner/repo, values: [example-owner/example] }
shared_secrets:
  - { secret: shared_key, server: other-server }
""".replace("run: { entrypoint: example-mcp }", "")
        + """\
run:
  entrypoint: example-mcp
  args: [stdio, --read-only]
  env:
    EXAMPLE_URL: https://example.invalid
    EXAMPLE_TOKEN: secret:shared_key
  state_dir: true
  state_dir_env: EXAMPLE_STATE
""",
    ),
    # --- unknown fields (contract 01 §7 rule 1) ---
    _case("unknown-top-near", HEAD + "identy: a typo of a known field\n"),
    _case("unknown-top-far", HEAD + "zzzzqqq: 1\n"),
    _case("unknown-install", _install(source="agent-mcp", pakage="x", zzzz="1")),
    _case("unknown-run", _run("{ entrypoint: example-mcp, arg: [x], zzzz: 1 }")),
    _case(
        "unknown-nested",
        HEAD
        + "tools:\n  - { name: a, description: b, writ: true }\n"
        + "arg_allows:\n  - { tools: all, arg: a, values: [b], value: c }\n"
        + "arg_denies:\n  - { tools: all, arg: a, values: [b], tool: c }\n"
        + "shared_secrets:\n  - { secret: a, server: other-server, servr: c }\n",
    ),
    # --- wrong type and missing ---
    _case(
        "wrong-type-top", "name: 12\nidentity: [x]\ninstall: pypi\nrun: example-mcp\ntools: {}\n"
    ),
    _case(
        "wrong-type-depth",
        HEAD.replace("run: { entrypoint: example-mcp }", "")
        + "run: { entrypoint: 5, args: stdio, env: [A], state_dir: maybe, state_dir_env: 7 }\n"
        + "tools:\n  - name-only\n  - { name: 5, description: [x], write: 2 }\n"
        + "arg_allows:\n  - { tools: 5, arg: 6, values: all }\n"
        + "arg_denies: all\n"
        + "shared_secrets:\n  - [a, b]\n",
    ),
    _case("wrong-type-env", _run("{ entrypoint: example-mcp, env: { A: 1, 5: x, B: ~ } }")),
    _case("missing-top", "name: %NAME%\n"),
    _case(
        "missing-nested",
        "name: %NAME%\nidentity: x\ninstall: {}\nrun: {}\n"
        "tools:\n  - {}\narg_allows:\n  - {}\nshared_secrets:\n  - {}\n",
    ),
    _case("lax-coercions", HEAD + 'tools:\n  - { name: a, description: b, write: "yes" }\n'),
    _case("install-python-number", _install(source="agent-mcp", python="3.12")),
    _case("install-python-default", HEAD),
    # --- the document as a whole ---
    _case("doc-empty", ""),
    _case("doc-two", HEAD + "---\nname: second\n"),
    _case("doc-top-list", "- name: %NAME%\n"),
    _case("doc-bad-yaml", "name: %NAME%\n  identity: x\n"),
    Case("doc-not-utf8", b"name: doc-not-utf8\nidentity: \xff\xfe\n"),
    # --- name and identity (contract 01b §2) ---
    _case("rule-name-pattern", HEAD.replace("name: %NAME%", "name: Bad_Name")),
    _case("rule-name-directory", HEAD.replace("name: %NAME%", "name: another-name")),
    _case("rule-name-newline", HEAD.replace("name: %NAME%", 'name: "%NAME%\\n"')),
    _case("rule-identity-blank", HEAD.replace("One written server.", '"   "')),
    _case("rule-identity-max", HEAD.replace("One written server.", "x" * 300)),
    _case("rule-identity-long", HEAD.replace("One written server.", "x" * 301)),
    _case("rule-identity-collapse", HEAD.replace("One written server.", '"' + "x   " * 100 + '"')),
    # --- install (contract 01b §3) ---
    _case("rule-source", _install(source="docker")),
    _case("rule-source-case", _install(source="PyPI")),
    _case("rule-pypi-missing", _install(source="pypi")),
    _case("rule-release-missing", _install(source="github-release", repo="a/b")),
    _case(
        "rule-agent-mcp-extra",
        _install(source="agent-mcp", version="1.0.0", ref="main", package="x", lock="a/b"),
    ),
    _case("rule-pypi-extra", PYPI.replace("  lock:", f"  sha256: {DIGEST}\n  repo: a/b\n  lock:")),
    _case("rule-release-extra", RELEASE.replace("  version:", "  package: x\n  version:")),
    _case("rule-version-range", _pypi(version='">=1.0"')),
    _case("rule-version-letter", _pypi(version="v1.0.2")),
    _case("rule-version-star", _pypi(version='"1.*"')),
    _case("rule-version-space", _pypi(version='"1.0 2"')),
    _case("rule-version-empty", _pypi(version='""')),
    _case("rule-version-number", _pypi(version="1.0")),
    _case("rule-version-suffix", _pypi(version="1.0.2rc1")),
    _case("rule-sha256-short", RELEASE.replace(DIGEST, DIGEST[:63])),
    _case("rule-sha256-upper", RELEASE.replace(DIGEST, DIGEST.upper())),
    _case("rule-sha256-long", RELEASE.replace(DIGEST, DIGEST + "0")),
    _case("rule-repo", RELEASE.replace("example-owner/example-mcp", "example-owner")),
    _case("rule-repo-three", RELEASE.replace("example-owner/example-mcp", "a/b/c")),
    _case("rule-repo-space", RELEASE.replace("example-owner/example-mcp", '"a b/c"')),
    _case("rule-lock-parent", _pypi(lock="mcp/../x.lock")),
    _case("rule-lock-absolute", _pypi(lock="/etc/passwd")),
    _case("rule-lock-dotdot", _pypi(lock="../x.lock")),
    _case("rule-lock-slash", _pypi(lock="mcp/x/")),
    _case("rule-lock-double", _pypi(lock="mcp//x.lock")),
    _case("rule-lock-space", _pypi(lock='"mcp/a  b\\tc.lock"')),
    _case("rule-lock-long", _pypi(lock="a" * 257)),
    _case("rule-lock-max", _pypi(lock="a" * 256)),
    _case("rule-lock-shown", _pypi(lock='"' + "ab " * 40 + '"')),
    # --- run (contract 01b §4) ---
    _case("rule-entrypoint-path", _run("{ entrypoint: /usr/bin/example-mcp }")),
    _case("rule-entrypoint-empty", _run('{ entrypoint: "" }')),
    _case(
        "rule-env",
        _run(
            "{ entrypoint: example-mcp, env: { lower: x, EXAMPLE_TOKEN: literal, "
            'API_KEY: "secret:", OK_URL: "secret:", PASSWORD_FILE: secret:pw, '
            "MY_SECRET: x, A1_B: y, 1A: z } }"
        ),
    ),
    _case("rule-env-secret-ok", _run("{ entrypoint: example-mcp, env: { A_TOKEN: secret:a } }")),
    _case("rule-state-dir-alone", _run("{ entrypoint: example-mcp, state_dir: true }")),
    _case("rule-state-env-alone", _run("{ entrypoint: example-mcp, state_dir_env: STATE }")),
    _case(
        "rule-state-dir-ok",
        _run("{ entrypoint: example-mcp, state_dir: true, state_dir_env: STATE }"),
    ),
    # --- tools (contract 01b §5) ---
    _case(
        "rule-tools",
        HEAD
        + "tools:\n"
        + "  - { name: read_one, description: Read. }\n"
        + "  - { name: read_one, description: Read again. }\n"
        + "  - { name: 1bad, description: Bad name. }\n"
        + '  - { name: has space, description: "   " }\n'
        + "  - { name: long_description, description: "
        + "x" * 201
        + " }\n"
        + "  - { name: max_description, description: "
        + "x" * 200
        + " }\n",
    ),
    _case("rule-tools-empty", HEAD + "tools: []\n"),
    # --- fences (contract 01b §7) ---
    _case(
        "rule-fences",
        HEAD
        + TOOLS
        + "arg_allows:\n"
        + "  - { tools: ALL, arg: owner, values: [a] }\n"
        + "  - { tools: [read_one, nope, also_nope], arg: owner, values: [a] }\n"
        + "  - { tools: [], arg: a/b/c, values: [a] }\n"
        + '  - { tools: all, arg: "1bad", values: [] }\n'
        + "arg_denies:\n"
        + '  - { tools: all, arg: "", values: [a] }\n'
        + "  - { tools: all, arg: a/, values: [a] }\n"
        + "  - { tools: [write_one], arg: _a/b_1, values: [a, a] }\n",
    ),
    _case("rule-fence-no-tools", HEAD + "arg_allows:\n  - { tools: [x], arg: a, values: [b] }\n"),
    # An argument name has 64 characters at most (contract 01b §7.2).
    _case(
        "rule-fence-arg-length",
        HEAD
        + TOOLS
        + "arg_denies:\n"
        + "  - { tools: all, arg: "
        + "a" * 64
        + "/"
        + "b" * 64
        + ", values: [a] }\n"
        + "  - { tools: all, arg: "
        + "a" * 65
        + ", values: [a] }\n"
        + "  - { tools: all, arg: owner/"
        + "b" * 65
        + ", values: [a] }\n",
    ),
    # `all` covers the tools that the file declares (contract 01b §7.1).
    _case(
        "rule-fence-all-no-tool",
        HEAD
        + "arg_allows:\n  - { tools: all, arg: a, values: [b] }\n"
        + "arg_denies:\n  - { tools: all, arg: a, values: [b] }\n",
    ),
    _case(
        "rule-fence-all-empty-tools",
        HEAD + "tools: []\narg_denies:\n  - { tools: all, arg: a, values: [b] }\n",
    ),
    # --- shared secrets (contract 01b §4.3) ---
    _case(
        "rule-shared",
        _run("{ entrypoint: example-mcp, env: { A_TOKEN: secret:shared_a, B: plain } }")
        + "shared_secrets:\n"
        + "  - { secret: shared_a, server: other-server }\n"
        + "  - { secret: shared_a, server: third-server }\n"
        + "  - { secret: not_named, server: other-server }\n"
        + "  - { secret: shared_a, server: %NAME% }\n"
        + "  - { secret: plain, server: Bad_Name }\n",
    ),
    # --- a literal under a name that looks like a credential (contract 01b §4.1) ---
    _case("rule-env-key-literal", _run("{ entrypoint: example-mcp, env: { API_KEY: literal } }")),
    _case(
        "rule-env-password-literal",
        _run("{ entrypoint: example-mcp, env: { DB_PASSWORD: literal } }"),
    ),
    # --- a version with a sign, and three texts past the limit of 64 characters ---
    _case("long-version-hyphen", _pypi(version="1.0.0-rc1")),
    _case("long-version-plus", _pypi(version="1.0.0+local")),
    _case("long-version", _pypi(version='"' + "1" * 65 + '"')),
    _case(
        "long-env-name",
        _run("{ entrypoint: example-mcp, env: { " + "A" * 65 + ": plain } }"),
    ),
    _case(
        "long-tool-name",
        HEAD + "tools:\n  - { name: " + "a" * 65 + ", description: One tool. }\n",
    ),
    # --- the two limits on merge keys, as for a family file ---
    _case("yaml-merge-chain-at-limit", merge_chain(_REPEATED, MERGE_CHAIN_MAX)),
    _case("yaml-merge-chain-past-limit", merge_chain(_REPEATED, MERGE_CHAIN_MAX + 1)),
    _case("yaml-merge-entries-at-limit", merged_entries(_REPEATED, MERGED_ENTRIES_MAX)),
    _case("yaml-merge-entries-past-limit", merged_entries(_REPEATED, MERGED_ENTRIES_MAX + 1)),
)
