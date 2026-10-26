"""Written `server.toml` inputs: one case of each rule, the cases of the TOML
reader and the cases of the grammar of root.

A case that has the id of a vector of the frozen file `server_file.json` is
the twin of that YAML input and holds the same values. The reader reads each
field in the strict mode, so a twin of a `lax-` input holds the text or the
float of its input, and the reader refuses it. The validator also applies
the grammar of root, so a twin can get another result than its input. A
YAML input that needs a form of YAML has no twin: a null, a key that is no
text, a second document, a top value that is no mapping, a text that is no
YAML and a merge key. The inputs with no twin are `wrong-type-env`,
`doc-two`, `doc-top-list`, `doc-bad-yaml` and the four `yaml-merge-` inputs.

The cases with an id that starts with `toml-` are the cases of the TOML
reader and of the TOML path: the refusals of the reader, the size limit, the
level limit, a form of TOML 1.1, a date-time, a value of a wrong kind at each
field, the order of the faults, two names of a server file in one directory,
each grammar that the TOML path applies and each cap at its limit and one
past it.

No case names a deployment.
"""

from __future__ import annotations

from typing import Final

from vectors.surfaces.family_toml_cases import PLACEHOLDER, Case

#: A 64-character lowercase hexadecimal digest.
DIGEST: Final = "0123456789abcdef" * 4

#: The most bytes of a server file. The case at the limit and the case one
#: byte past it hold this number as text, so a changed limit moves a vector.
SERVER_BYTES: Final = 65536

#: The most levels of a TOML text. The top table has level 1.
LEVELS: Final = 8

#: The caps of the grammar of root on a count of entries, and on the count of
#: the characters of one word of `run.args` and of one value. A case holds
#: each number as text, so a changed cap moves a vector.
MAX_TOOLS: Final = 200
MAX_ARGS: Final = 32
MAX_ENV: Final = 32
MAX_FENCES: Final = 64
MAX_FENCE_VALUES: Final = 256
MAX_SHARED: Final = 8
MAX_ARG_CHARS: Final = 256
MAX_VALUE_CHARS: Final = 256

#: The caps of the grammar of root on the length of a text.
IDENTITY_MAX: Final = 300
DESCRIPTION_MAX: Final = 200
PACKAGE_MAX: Final = 64
REPO_OWNER_MAX: Final = 39
REPO_NAME_MAX: Final = 100
ASSET_MAX: Final = 128
ENTRYPOINT_MAX: Final = 64
ENV_NAME_MAX: Final = 64
SECRET_KEY_MAX: Final = 63


def _case(
    case_id: str, text: str, directory: str = "", files: dict[str, str] | None = None
) -> Case:
    return Case(case_id, text.encode("utf-8"), directory, files or {})


def _replaced(text: str, old: str, new: str) -> str:
    """`text` with the one line `old` replaced. A case must not lose its
    change to a typo, so `old` must be in `text`."""
    if old not in text:
        raise ValueError(f"{old!r} is not in the text")

    return text.replace(old, new)


# An `agent-mcp` server every rule accepts, in the layout of a converted
# registry file. A case appends to it.
HEAD: Final = """\
name = "%NAME%"
identity = "One written server."
install.source = "agent-mcp"
run.entrypoint = "example-mcp"
"""

INSTALL_LINE: Final = 'install.source = "agent-mcp"'
RUN_LINE: Final = 'run.entrypoint = "example-mcp"'
IDENTITY_LINE: Final = 'identity = "One written server."'

PYPI: Final = """\
name = "%NAME%"
identity = "One written server."
install.source = "pypi"
install.package = "example-mcp"
install.version = "1.0.2"
install.lock = "mcp/%NAME%/install.lock"
run.entrypoint = "example-mcp"
"""

RELEASE: Final = f"""\
name = "%NAME%"
identity = "One written server."
install.source = "github-release"
install.repo = "example-owner/example-mcp"
install.version = "1.12.0"
install.asset = "example-mcp_Linux_x86_64.tar.gz"
install.sha256 = "{DIGEST}"
run.entrypoint = "example-mcp"
"""

TOOLS: Final = """\
tools = [
    { name = "read_one", description = "Read one thing." },
    { name = "write_one", description = "Write one thing.", write = true },
]
"""


def _install(block: str) -> str:
    """`HEAD` with another `install` table. `block` is TOML text."""
    return _replaced(HEAD, INSTALL_LINE, f"install = {{ {block} }}")


def _run(block: str) -> str:
    """`HEAD` with another `run` table. `block` is TOML text."""
    return _replaced(HEAD, RUN_LINE, f"run = {{ {block} }}")


def _pypi(key: str, value: str) -> str:
    """`PYPI` with the value of one key of `install` replaced. `value` is
    TOML text."""
    out: list[str] = []
    for line in PYPI.splitlines():
        name, _, _ = line.partition(" = ")
        out.append(f"{name} = {value}" if name == f"install.{key}" else line)

    return "\n".join(out) + "\n"


def _release(key: str, value: str) -> str:
    """`RELEASE` with the value of one key of `install` replaced."""
    out: list[str] = []
    for line in RELEASE.splitlines():
        name, _, _ = line.partition(" = ")
        out.append(f"{name} = {value}" if name == f"install.{key}" else line)

    return "\n".join(out) + "\n"


def _identity(value: str) -> str:
    """`HEAD` with another identity. `value` is TOML text."""
    return _replaced(HEAD, IDENTITY_LINE, f"identity = {value}")


def _quoted(text: str) -> str:
    """A TOML basic string of a text that holds no `"` and no `\\`."""
    return f'"{text}"'


def _lines(items: list[str]) -> str:
    """An array of TOML values, one item on each line."""
    return "[\n" + "".join(f"    {item},\n" for item in items) + "]"


TWINS: Final[tuple[Case, ...]] = (
    # --- the control: nothing wrong ---------------------------------------
    _case("ok-agent-mcp", HEAD),
    _case("ok-pypi", PYPI),
    _case("ok-release", RELEASE),
    _case(
        "ok-every-field",
        _replaced(RELEASE, RUN_LINE, "")
        + """\
tools = [
    { name = "read_one", description = "Read one thing.", write = false },
    { name = "Write-Two_2", description = "Write one thing.", write = true },
]
arg_allows = [
    { tools = "all", arg = "owner", values = ["example-owner"] },
]
arg_denies = [
    { tools = ["read_one"], arg = "owner/repo", values = ["example-owner/example"] },
]
shared_secrets = [
    { secret = "shared_key", server = "other-server" },
]
run.entrypoint = "example-mcp"
run.args = ["stdio", "--read-only"]
run.env.EXAMPLE_URL = "https://example.invalid"
run.env.EXAMPLE_TOKEN = "secret:shared_key"
run.state_dir = true
run.state_dir_env = "EXAMPLE_STATE"
""",
    ),
    # --- unknown fields (contract 01 §7 rule 1) ----------------------------
    _case("unknown-top-near", HEAD + 'identy = "a typo of a known field"\n'),
    _case("unknown-top-far", HEAD + "zzzzqqq = 1\n"),
    _case("unknown-install", _install('source = "agent-mcp", pakage = "x", zzzz = 1')),
    _case("unknown-run", _run('entrypoint = "example-mcp", arg = ["x"], zzzz = 1')),
    _case(
        "unknown-nested",
        HEAD
        + """\
tools = [
    { name = "a", description = "b", writ = true },
]
arg_allows = [
    { tools = "all", arg = "a", values = ["b"], value = "c" },
]
arg_denies = [
    { tools = "all", arg = "a", values = ["b"], tool = "c" },
]
shared_secrets = [
    { secret = "a", server = "other-server", servr = "c" },
]
""",
    ),
    # --- wrong type and missing -------------------------------------------
    _case(
        "wrong-type-top",
        'name = 12\nidentity = ["x"]\ninstall = "pypi"\nrun = "example-mcp"\ntools = {}\n',
    ),
    _case(
        "wrong-type-depth",
        _replaced(HEAD, RUN_LINE + "\n", "")
        + """\
run = { entrypoint = 5, args = "stdio", env = ["A"], state_dir = "maybe", state_dir_env = 7 }
tools = [
    "name-only",
    { name = 5, description = ["x"], write = 2 },
]
arg_allows = [
    { tools = 5, arg = 6, values = "all" },
]
arg_denies = "all"
shared_secrets = [
    ["a", "b"],
]
""",
    ),
    _case("missing-top", 'name = "%NAME%"\n'),
    _case(
        "missing-nested",
        """\
name = "%NAME%"
identity = "x"
install = {}
run = {}
tools = [
    {},
]
arg_allows = [
    {},
]
shared_secrets = [
    {},
]
""",
    ),
    _case(
        "lax-coercions",
        HEAD + 'tools = [\n    { name = "a", description = "b", write = "yes" },\n]\n',
    ),
    _case("install-python-number", _install('source = "agent-mcp", python = 3.12')),
    _case("install-python-default", HEAD),
    # --- the document as a whole --------------------------------------------
    _case("doc-empty", ""),
    Case("doc-not-utf8", b'name = "doc-not-utf8"\nidentity = "\xff\xfe"\n'),
    # --- name and identity (contract 01b §2) --------------------------------
    _case("rule-name-pattern", _replaced(HEAD, 'name = "%NAME%"', 'name = "Bad_Name"')),
    _case("rule-name-directory", _replaced(HEAD, 'name = "%NAME%"', 'name = "another-name"')),
    _case("rule-name-newline", _replaced(HEAD, 'name = "%NAME%"', 'name = "%NAME%\\n"')),
    _case("rule-identity-blank", _identity('"   "')),
    _case("rule-identity-max", _identity(_quoted("x" * IDENTITY_MAX))),
    _case("rule-identity-long", _identity(_quoted("x" * (IDENTITY_MAX + 1)))),
    _case("rule-identity-collapse", _identity(_quoted("x   " * 100))),
    # --- install (contract 01b §3) ------------------------------------------
    _case("rule-source", _install('source = "docker"')),
    _case("rule-source-case", _install('source = "PyPI"')),
    _case("rule-pypi-missing", _install('source = "pypi"')),
    _case("rule-release-missing", _install('source = "github-release", repo = "a/b"')),
    _case(
        "rule-agent-mcp-extra",
        _install(
            'source = "agent-mcp", version = "1.0.0", ref = "main", package = "x", lock = "a/b"'
        ),
    ),
    _case(
        "rule-pypi-extra",
        _replaced(
            PYPI,
            "install.lock =",
            f'install.sha256 = "{DIGEST}"\ninstall.repo = "a/b"\ninstall.lock =',
        ),
    ),
    _case(
        "rule-release-extra",
        _replaced(RELEASE, "install.version =", 'install.package = "x"\ninstall.version ='),
    ),
    _case("rule-version-range", _pypi("version", '">=1.0"')),
    _case("rule-version-letter", _pypi("version", '"v1.0.2"')),
    _case("rule-version-star", _pypi("version", '"1.*"')),
    _case("rule-version-space", _pypi("version", '"1.0 2"')),
    _case("rule-version-empty", _pypi("version", '""')),
    _case("rule-version-number", _pypi("version", "1.0")),
    _case("rule-version-suffix", _pypi("version", '"1.0.2rc1"')),
    _case("rule-sha256-short", _release("sha256", _quoted(DIGEST[:63]))),
    _case("rule-sha256-upper", _release("sha256", _quoted(DIGEST.upper()))),
    _case("rule-sha256-long", _release("sha256", _quoted(DIGEST + "0"))),
    _case("rule-repo", _release("repo", '"example-owner"')),
    _case("rule-repo-three", _release("repo", '"a/b/c"')),
    _case("rule-repo-space", _release("repo", '"a b/c"')),
    _case("rule-lock-parent", _pypi("lock", '"mcp/../x.lock"')),
    _case("rule-lock-absolute", _pypi("lock", '"/etc/passwd"')),
    _case("rule-lock-dotdot", _pypi("lock", '"../x.lock"')),
    _case("rule-lock-slash", _pypi("lock", '"mcp/x/"')),
    _case("rule-lock-double", _pypi("lock", '"mcp//x.lock"')),
    _case("rule-lock-space", _pypi("lock", '"mcp/a  b\\tc.lock"')),
    _case("rule-lock-long", _pypi("lock", _quoted("a" * 257))),
    _case("rule-lock-max", _pypi("lock", _quoted("a" * 256))),
    _case("rule-lock-shown", _pypi("lock", _quoted("ab " * 40))),
    # --- run (contract 01b §4) ----------------------------------------------
    _case("rule-entrypoint-path", _run('entrypoint = "/usr/bin/example-mcp"')),
    _case("rule-entrypoint-empty", _run('entrypoint = ""')),
    _case(
        "rule-env",
        _run(
            'entrypoint = "example-mcp", env = { lower = "x", EXAMPLE_TOKEN = "literal", '
            'API_KEY = "secret:", OK_URL = "secret:", PASSWORD_FILE = "secret:pw", '
            'MY_SECRET = "x", A1_B = "y", 1A = "z" }'
        ),
    ),
    _case("rule-env-secret-ok", _run('entrypoint = "example-mcp", env = { A_TOKEN = "secret:a" }')),
    _case("rule-state-dir-alone", _run('entrypoint = "example-mcp", state_dir = true')),
    _case("rule-state-env-alone", _run('entrypoint = "example-mcp", state_dir_env = "STATE"')),
    _case(
        "rule-state-dir-ok",
        _run('entrypoint = "example-mcp", state_dir = true, state_dir_env = "STATE"'),
    ),
    # --- tools (contract 01b §5) --------------------------------------------
    _case(
        "rule-tools",
        HEAD
        + "tools = "
        + _lines(
            [
                '{ name = "read_one", description = "Read." }',
                '{ name = "read_one", description = "Read again." }',
                '{ name = "1bad", description = "Bad name." }',
                '{ name = "has space", description = "   " }',
                '{ name = "long_description", description = ' + _quoted("x" * 201) + " }",
                '{ name = "max_description", description = ' + _quoted("x" * 200) + " }",
            ]
        )
        + "\n",
    ),
    _case("rule-tools-empty", HEAD + "tools = []\n"),
    # --- fences (contract 01b §7) -------------------------------------------
    _case(
        "rule-fences",
        HEAD
        + TOOLS
        + """\
arg_allows = [
    { tools = "ALL", arg = "owner", values = ["a"] },
    { tools = ["read_one", "nope", "also_nope"], arg = "owner", values = ["a"] },
    { tools = [], arg = "a/b/c", values = ["a"] },
    { tools = "all", arg = "1bad", values = [] },
]
arg_denies = [
    { tools = "all", arg = "", values = ["a"] },
    { tools = "all", arg = "a/", values = ["a"] },
    { tools = ["write_one"], arg = "_a/b_1", values = ["a", "a"] },
]
""",
    ),
    _case(
        "rule-fence-no-tools",
        HEAD + 'arg_allows = [\n    { tools = ["x"], arg = "a", values = ["b"] },\n]\n',
    ),
    _case(
        "rule-fence-arg-length",
        HEAD
        + TOOLS
        + "arg_denies = "
        + _lines(
            [
                f'{{ tools = "all", arg = "{"a" * 64}/{"b" * 64}", values = ["a"] }}',
                f'{{ tools = "all", arg = "{"a" * 65}", values = ["a"] }}',
                f'{{ tools = "all", arg = "owner/{"b" * 65}", values = ["a"] }}',
            ]
        )
        + "\n",
    ),
    _case(
        "rule-fence-all-no-tool",
        HEAD
        + 'arg_allows = [\n    { tools = "all", arg = "a", values = ["b"] },\n]\n'
        + 'arg_denies = [\n    { tools = "all", arg = "a", values = ["b"] },\n]\n',
    ),
    _case(
        "rule-fence-all-empty-tools",
        HEAD
        + "tools = []\n"
        + 'arg_denies = [\n    { tools = "all", arg = "a", values = ["b"] },\n]\n',
    ),
    # --- shared secrets (contract 01b §4.3) ---------------------------------
    _case(
        "rule-shared",
        _run('entrypoint = "example-mcp", env = { A_TOKEN = "secret:shared_a", B = "plain" }')
        + "shared_secrets = "
        + _lines(
            [
                '{ secret = "shared_a", server = "other-server" }',
                '{ secret = "shared_a", server = "third-server" }',
                '{ secret = "not_named", server = "other-server" }',
                '{ secret = "shared_a", server = "%NAME%" }',
                '{ secret = "plain", server = "Bad_Name" }',
            ]
        )
        + "\n",
    ),
    # --- a literal under a name that looks like a credential ------------------
    _case(
        "rule-env-key-literal", _run('entrypoint = "example-mcp", env = { API_KEY = "literal" }')
    ),
    _case(
        "rule-env-password-literal",
        _run('entrypoint = "example-mcp", env = { DB_PASSWORD = "literal" }'),
    ),
    # --- a version with a sign, and three texts past the limit of 64 ----------
    _case("long-version-hyphen", _pypi("version", '"1.0.0-rc1"')),
    _case("long-version-plus", _pypi("version", '"1.0.0+local"')),
    _case("long-version", _pypi("version", _quoted("1" * 65))),
    _case(
        "long-env-name",
        _run(f'entrypoint = "example-mcp", env = {{ {"A" * 65} = "plain" }}'),
    ),
    _case(
        "long-tool-name",
        HEAD + f'tools = [\n    {{ name = "{"a" * 65}", description = "One tool." }},\n]\n',
    ),
)


#: The text of a YAML server file, for the case of a `server.yaml` beside a
#: `server.toml`. The registry reads no `server.yaml`.
YAML_SERVER: Final = """\
name: toml-two-names
identity: One written server.
install: { source: agent-mcp }
run: { entrypoint: example-mcp }
"""


def _padded(case_id: str, size: int) -> Case:
    """A valid server file of `size` bytes: `HEAD` and one comment line."""
    head = HEAD.replace(PLACEHOLDER, case_id)
    comment = "#" * (size - len(head.encode("utf-8")) - 1)

    return Case(case_id, f"{head}{comment}\n".encode())


def _nested(lists: int) -> str:
    """`HEAD` and an `arg_allows` of `lists` lists, each inside the one
    before."""
    return HEAD + "arg_allows = " + "[" * lists + '"x"' + "]" * lists + "\n"


def _tools(count: int) -> str:
    """`HEAD` with `count` tools."""
    items = [f'{{ name = "tool_{index}", description = "One tool." }}' for index in range(count)]

    return HEAD + "tools = " + _lines(items) + "\n"


def _args(count: int) -> str:
    """`HEAD` with `count` words of `run.args`."""
    words = ", ".join(_quoted(f"a{index}") for index in range(count))

    return _run(f'entrypoint = "example-mcp", args = [{words}]')


def _env(count: int) -> str:
    """`HEAD` with `count` variables of `run.env`."""
    pairs = ", ".join(f'V{index} = "x"' for index in range(count))

    return _run(f'entrypoint = "example-mcp", env = {{ {pairs} }}')


def _fences(field: str, count: int) -> str:
    """`HEAD` and `TOOLS` with `count` fences of `field`."""
    item = '{ tools = "all", arg = "owner", values = ["a"] }'

    return HEAD + TOOLS + f"{field} = " + _lines([item] * count) + "\n"


def _fence_values(values: list[str]) -> str:
    """`HEAD` and `TOOLS` with one fence of `arg_denies` that holds `values`.
    Each value is TOML text."""
    joined = ", ".join(values)

    return (
        HEAD
        + TOOLS
        + f'arg_denies = [\n    {{ tools = "all", arg = "a", values = [{joined}] }},\n]\n'
    )


def _shared(count: int) -> str:
    """`HEAD` with `count` secrets that `run.env` names and that one other
    server shares."""
    env = ", ".join(f'S{index}_TOKEN = "secret:shared_{index}"' for index in range(count))
    items = [f'{{ secret = "shared_{index}", server = "other-server" }}' for index in range(count)]

    return (
        _run(f'entrypoint = "example-mcp", env = {{ {env} }}')
        + "shared_secrets = "
        + _lines(items)
        + "\n"
    )


def _secret_key(key: str) -> str:
    """`HEAD` with one variable whose value names the secret `key`."""
    return _run(f'entrypoint = "example-mcp", env = {{ A_TOKEN = "secret:{key}" }}')


def _repo(owner: str, name: str) -> str:
    return _release("repo", _quoted(f"{owner}/{name}"))


#: The cases of the TOML reader and of the loader of a TOML file.
READER_CASES: Final[tuple[Case, ...]] = (
    # --- the refusals of the reader, and the size limit ----------------------
    _padded("toml-size-at-limit", SERVER_BYTES),
    _padded("toml-size-past-limit", SERVER_BYTES + 1),
    _case("toml-byte-order-mark", "﻿" + HEAD),
    _case("toml-dup-key", HEAD + 'name = "%NAME%"\n'),
    # --- TOML 1.1: a comma before the end of an inline table ---------------
    _case("toml-1-1-comma", _install('source = "agent-mcp", ')),
    # --- the level limit -------------------------------------------------------
    _case("toml-levels-at-limit", _nested(LEVELS - 1)),
    _case("toml-levels-past-limit", _nested(LEVELS)),
    # --- tables and a list of tables -------------------------------------------
    _case(
        "toml-table-headers",
        """\
name = "%NAME%"
identity = "One written server."

[install]
source = "agent-mcp"
python = "3.13"

[run]
entrypoint = "example-mcp"
args = ["stdio"]
state_dir = true
state_dir_env = "EXAMPLE_STATE"

[run.env]
EXAMPLE_TOKEN = "secret:example_token"

[[tools]]
name = "read_one"
description = "Read one thing."

[[arg_denies]]
tools = ["read_one"]
arg = "owner"
values = ["example-owner"]
""",
    ),
    # --- a date-time: no field takes one ----------------------------------------
    _case("toml-date-text", _identity("2026-10-03")),
    _case("toml-date-table", 'name = "%NAME%"\nidentity = "x"\ninstall = 1979-05-27T07:32:00Z\n'),
    _case("toml-time-boolean", _run('entrypoint = "example-mcp", state_dir = 07:32:00')),
    _case("toml-time-list", _run('entrypoint = "example-mcp", args = [07:32:00]')),
    # --- a value of a wrong kind at each field, at each nested table and at
    # each leaf. A wrong kind at a table hides each fault below it, so each
    # depth has a case of its own. ------------------------------------------------
    _case(
        "toml-wrong-type-each-field",
        """\
name = true
identity = 1.5
install = []
run = 1
tools = { name = "a" }
arg_allows = 1
arg_denies = { tools = "all" }
shared_secrets = "x"
""",
    ),
    _case(
        "toml-wrong-type-each-block",
        HEAD
        + """\
tools = [1, "x", []]
arg_allows = [true]
arg_denies = [1.5]
shared_secrets = ["x"]
""",
    ),
    _case(
        "toml-wrong-type-each-leaf",
        """\
name = "%NAME%"
identity = "One written server."
install = { source = 1, package = 1, version = 1, lock = 1, python = 1, repo = 1, asset = 1, \
sha256 = 1, ref = 1 }
run = { entrypoint = 1, args = [1, true], env = 1, state_dir = 1, state_dir_env = 1 }
tools = [
    { name = 1, description = 1, write = "yes" },
]
arg_allows = [
    { tools = [1], arg = 1, values = [1] },
]
arg_denies = [
    { tools = true, arg = "a", values = 1 },
]
shared_secrets = [
    { secret = 1, server = 1 },
]
""",
    ),
    _case(
        "toml-missing-fence-tools", HEAD + 'arg_denies = [\n    { arg = "a", values = ["b"] },\n]\n'
    ),
    # --- the order of the faults: the reader sorts the keys of a table ---------
    _case("toml-unknown-reverse", HEAD + "zeta = 1\nalpha = 2\n"),
    _case(
        "toml-env-reverse",
        _run(
            'entrypoint = "example-mcp", env = { ZZ = 1, BB = true, Z_TOKEN = "literal", '
            'A_TOKEN = "literal" }'
        ),
    ),
    _case(
        "toml-env-rules-reverse",
        _run('entrypoint = "example-mcp", env = { Z_TOKEN = "literal", A_TOKEN = "literal" }'),
    ),
    # --- two names of a server file in one directory ----------------------------
    _case(
        "toml-two-names",
        HEAD,
        files={"mcp/toml-two-names/server.yaml": YAML_SERVER},
    ),
)

#: One case for each rule of the grammar of root that contract 01b does not
#: give. A cap has a case at its limit and one past it.
GRAMMAR_CASES: Final[tuple[Case, ...]] = (
    # --- the count of the characters of an identity and of a description -----
    _case("toml-identity-count-at-limit", _identity(_quoted("x " * (IDENTITY_MAX // 2)))),
    _case(
        "toml-identity-count-past-limit",
        _identity(_quoted("x " * (IDENTITY_MAX // 2) + "x")),
    ),
    _case(
        "toml-description-at-limit",
        HEAD
        + "tools = "
        + _lines([f'{{ name = "a", description = "{"x " * (DESCRIPTION_MAX // 2)}" }}'])
        + "\n",
    ),
    _case(
        "toml-description-past-limit",
        HEAD
        + "tools = "
        + _lines([f'{{ name = "a", description = "{"x " * (DESCRIPTION_MAX // 2)}x" }}'])
        + "\n",
    ),
    _case(
        "toml-description-blank",
        HEAD + 'tools = [\n    { name = "a", description = "\\t \\n" },\n]\n',
    ),
    # --- the seven field grammars ------------------------------------------------
    _case("toml-package-grammar", _pypi("package", '".example"')),
    _case("toml-package-space", _pypi("package", '"example mcp"')),
    _case("toml-package-at-limit", _pypi("package", _quoted("p" * PACKAGE_MAX))),
    _case("toml-package-past-limit", _pypi("package", _quoted("p" * (PACKAGE_MAX + 1)))),
    _case("toml-version-grammar", _pypi("version", '"1.0.2+local"')),
    _case("toml-repo-grammar", _release("repo", '"-owner/example-mcp"')),
    _case("toml-repo-name-grammar", _release("repo", '"example-owner/.example"')),
    _case("toml-repo-at-limit", _repo("o" * REPO_OWNER_MAX, "n" * REPO_NAME_MAX)),
    _case("toml-repo-owner-past-limit", _repo("o" * (REPO_OWNER_MAX + 1), "n")),
    _case("toml-repo-name-past-limit", _repo("o", "n" * (REPO_NAME_MAX + 1))),
    _case("toml-asset-grammar", _release("asset", '"_example.tar.gz"')),
    _case("toml-asset-plus", _release("asset", '"example+linux.tar.gz"')),
    _case("toml-asset-at-limit", _release("asset", _quoted("a" * ASSET_MAX))),
    _case("toml-asset-past-limit", _release("asset", _quoted("a" * (ASSET_MAX + 1)))),
    _case("toml-python-old", _install('source = "agent-mcp", python = "3.11"')),
    _case("toml-python-newer", _install('source = "agent-mcp", python = "3.13"')),
    _case("toml-python-word", _install('source = "agent-mcp", python = "python3"')),
    _case("toml-entrypoint-grammar", _run('entrypoint = "example mcp"')),
    _case("toml-entrypoint-dot", _run('entrypoint = ".example"')),
    _case("toml-entrypoint-at-limit", _run(f'entrypoint = "{"e" * ENTRYPOINT_MAX}"')),
    _case("toml-entrypoint-past-limit", _run(f'entrypoint = "{"e" * (ENTRYPOINT_MAX + 1)}"')),
    _case(
        "toml-state-dir-env-grammar",
        _run('entrypoint = "example-mcp", state_dir = true, state_dir_env = "state"'),
    ),
    _case(
        "toml-state-dir-env-past-limit",
        _run(
            'entrypoint = "example-mcp", state_dir = true, '
            f'state_dir_env = "{"S" * (ENV_NAME_MAX + 1)}"'
        ),
    ),
    # --- a variable name, a value and a secret key --------------------------------
    _case(
        "toml-env-name-at-limit",
        _run(f'entrypoint = "example-mcp", env = {{ {"A" * ENV_NAME_MAX} = "plain" }}'),
    ),
    _case(
        "toml-env-value-at-limit",
        _run(f'entrypoint = "example-mcp", env = {{ A = "{"v" * MAX_VALUE_CHARS}" }}'),
    ),
    _case(
        "toml-env-value-past-limit",
        _run(f'entrypoint = "example-mcp", env = {{ A = "{"v" * (MAX_VALUE_CHARS + 1)}" }}'),
    ),
    _case(
        "toml-env-value-nul",
        _run('entrypoint = "example-mcp", env = { A = "a\\u0000b", B_TOKEN = "secret:\\u0000" }'),
    ),
    _case("toml-secret-key-upper", _secret_key("Example_key")),
    _case("toml-secret-key-short", _secret_key("k")),
    _case("toml-secret-key-at-limit", _secret_key("k" * SECRET_KEY_MAX)),
    _case("toml-secret-key-past-limit", _secret_key("k" * (SECRET_KEY_MAX + 1))),
    # --- a fence with no tool --------------------------------------------------------
    _case(
        "toml-fence-no-tool",
        HEAD + TOOLS + 'arg_denies = [\n    { tools = [], arg = "a", values = ["b"] },\n]\n',
    ),
    # --- each cap on a count, at its limit and one past it ----------------------------
    _case("toml-tools-at-limit", _tools(MAX_TOOLS)),
    _case("toml-tools-past-limit", _tools(MAX_TOOLS + 1)),
    _case("toml-args-at-limit", _args(MAX_ARGS)),
    _case("toml-args-past-limit", _args(MAX_ARGS + 1)),
    _case("toml-env-at-limit", _env(MAX_ENV)),
    _case("toml-env-past-limit", _env(MAX_ENV + 1)),
    _case("toml-allows-at-limit", _fences("arg_allows", MAX_FENCES)),
    _case("toml-allows-past-limit", _fences("arg_allows", MAX_FENCES + 1)),
    _case("toml-denies-at-limit", _fences("arg_denies", MAX_FENCES)),
    _case("toml-denies-past-limit", _fences("arg_denies", MAX_FENCES + 1)),
    _case(
        "toml-fence-values-at-limit",
        _fence_values([_quoted(f"v{index}") for index in range(MAX_FENCE_VALUES)]),
    ),
    _case(
        "toml-fence-values-past-limit",
        _fence_values([_quoted(f"v{index}") for index in range(MAX_FENCE_VALUES + 1)]),
    ),
    _case("toml-shared-at-limit", _shared(MAX_SHARED)),
    _case("toml-shared-past-limit", _shared(MAX_SHARED + 1)),
    # --- each cap on the characters of one word and of one value ------------------------
    _case(
        "toml-arg-word-at-limit",
        _run(f'entrypoint = "example-mcp", args = ["{"w" * MAX_ARG_CHARS}"]'),
    ),
    _case(
        "toml-arg-word-past-limit",
        _run(f'entrypoint = "example-mcp", args = ["{"w" * (MAX_ARG_CHARS + 1)}", "a\\u0000b"]'),
    ),
    _case(
        "toml-fence-value-at-limit",
        _fence_values([_quoted("v" * MAX_VALUE_CHARS)]),
    ),
    _case(
        "toml-fence-value-past-limit",
        _fence_values([_quoted("v" * (MAX_VALUE_CHARS + 1)), '"a\\u0000b"']),
    ),
    # --- a cap counts characters and not bytes -------------------------------------------
    _case(
        "toml-arg-word-characters",
        _run(f'entrypoint = "example-mcp", args = ["{"é" * MAX_ARG_CHARS}"]'),
    ),
)

CASES: Final[tuple[Case, ...]] = (*TWINS, *READER_CASES, *GRAMMAR_CASES)
