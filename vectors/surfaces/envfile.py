"""The reader of the env files (the group `envfile`).

Three surfaces of `creche_env.envfile`. `envfile.layer`: a pair, a `.env`, a
password database and an environment to the settings or to the faults.
`envfile.raw`: one file in the grammar-only mode. `envfile.trust`: the
owner and the mode of a file and the account that reads it.

Each pair here is written for a vector, so a new key of the product moves
no vector. The vectors use no file and no account of the machine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from creche_env.envfile import (
    MAX_BYTES,
    EnvError,
    Text,
    check_environment,
    check_trust,
    load,
    raw,
    read_pair,
)

from vectors.core import (
    ACCEPTED,
    REFUSED,
    Json,
    Raised,
    Surface,
    Vector,
    accepted,
    attempt,
    bytes_input,
    expand,
    refused,
    repeat_input,
    text_input,
)

GROUP: Final = "envfile"
CONTRACT: Final = "env/AGENTS.md, the env files"

#: The names that a fault gives the files of a vector.
DEFAULT_NAME: Final = "default.env"
EXAMPLE_NAME: Final = "example.env"
ENV_NAME: Final = ".env"
PLAIN_NAME: Final = "plain.env"
TRUSTED_NAME: Final = "site.env"

#: One file of a vector: a text, its bytes, or a long text as parts.
type Body = str | bytes | tuple[tuple[str, int], ...]

#: A written pair and the `.env` that completes it.
DEFAULTS: Final = (
    "# The defaults of a written pair.\n"
    "DATA_ROOT=/srv/data\n"
    "STATE_ROOT=${DATA_ROOT}/state\n"
    "OWNER_HOME=${home:OWNER_USER}\n"
    "OWNER_BIN=${OWNER_HOME}/bin\n"
    "PEER_URL=http://${LAN_ADDRESS}:8300\n"
    "OPTIONAL=\n"
)
EXAMPLE: Final = "# The required keys.\n#OWNER_USER=operator\n#LAN_ADDRESS=192.0.2.10\n"
ENV: Final = "OWNER_USER=operator\nLAN_ADDRESS=192.0.2.10\n"
HOMES: Final = (("operator", "/home/operator"),)


def _chain(count: int) -> str:
    """A chain of `count` references: `C0=${C1}`, and so on, to `C<count>=end`."""
    links = "".join(f"C{index}=${{C{index + 1}}}\n" for index in range(count))

    return f"{links}C{count}=end\n"


def _reversed_chain(count: int) -> str:
    return "".join(reversed(_chain(count).splitlines(keepends=True)))


def _cycle(count: int) -> str:
    """A cycle of `count` keys: `R0=${R1}`, and so on, to `R<count - 1>=${R0}`."""
    return "".join(f"R{index}=${{R{(index + 1) % count}}}\n" for index in range(count))


@dataclass(frozen=True)
class Layer:
    """One input of `envfile.layer`, and the result that its list names."""

    id: str
    wanted: str
    default: Body = DEFAULTS
    example: Body = EXAMPLE
    env: Body = ENV
    homes: tuple[tuple[str, str], ...] = HOMES
    environ: tuple[tuple[str, str], ...] = ()


#: The two results that a list of inputs names.
OK: Final = ACCEPTED
NO: Final = REFUSED


def _long(head: str, fill: str, total: int) -> tuple[tuple[str, int], ...]:
    """`head`, then one comment line of `fill` to `total` bytes."""
    count = total - len(head.encode("utf-8")) - 2

    return ((head + "#", 1), (fill, count), ("\n", 1))


SIGNS: Final = "!%&()*+,-./:;<=>?@[]^_`{|}~"

LAYERS: Final[tuple[Layer, ...]] = (
    # --- accepted: each layer ---
    Layer("the-written-pair", OK),
    Layer("env-overrides-a-root", OK, env=ENV + "DATA_ROOT=/data\n"),
    Layer("env-overrides-a-reference", OK, env=ENV + "STATE_ROOT=/state\n"),
    Layer("env-sets-an-optional-key", OK, env=ENV + "OPTIONAL=on\n"),
    Layer(
        "env-sets-the-home-and-asks-no-account", OK, env=ENV + "OWNER_HOME=/home/other\n", homes=()
    ),
    Layer(
        "home-of-another-account",
        OK,
        env="OWNER_USER=builder\nLAN_ADDRESS=192.0.2.10\n",
        homes=(("builder", "/var/lib/builder"), ("operator", "/home/operator")),
    ),
    Layer("home-in-a-longer-value", OK, default=DEFAULTS + "LOGS=${home:OWNER_USER}/logs\n"),
    Layer("reference-to-a-later-key", OK, default=DEFAULTS + "FIRST=${LAST}/x\nLAST=/last\n"),
    Layer(
        "two-references-in-one-value", OK, default=DEFAULTS + "TWICE=${DATA_ROOT}:${STATE_ROOT}\n"
    ),
    Layer("chain-of-eight", OK, default=_chain(8), example="", env=""),
    Layer("chain-of-eight-in-reverse", OK, default=_reversed_chain(8), example="", env=""),
    Layer("empty-files", OK, default="", example="", env=""),
    Layer("line-ends-only", OK, default="\n", example="\n", env="\n"),
    Layer(
        "comments-and-empty-lines",
        OK,
        default="#\n\n# a\n#NOT_A_KEY=1\n" + DEFAULTS + "\n#\n",
        example="#\n\n" + EXAMPLE + "# The end.\n",
        env="# The host.\n\n#DATA_ROOT=/other\n" + ENV + "\n",
    ),
    Layer("comment-outside-ascii", OK, env="# caf\u00e9 \u2603\n" + ENV),
    Layer("each-printable-sign", OK, default=DEFAULTS + f"SIGNS=a{SIGNS}z\n"),
    Layer("inner-spaces-and-equals-signs", OK, env=ENV + "DATA_ROOT=/a b  c=d==\n"),
    Layer("braces-with-no-dollar", OK, default=DEFAULTS + "BRACES={x} {} }{\n"),
    Layer("longest-key", OK, default=DEFAULTS + "K" + "_" * 63 + "=1\n"),
    Layer("key-of-one-letter-and-digits", OK, default=DEFAULTS + "A=1\nA1_2=2\n"),
    Layer("largest-file", OK, env=_long(ENV, "x", MAX_BYTES)),
    Layer(
        "environment-equal", OK, environ=(("DATA_ROOT", "/srv/data"), ("OWNER_USER", "operator"))
    ),
    Layer(
        "environment-equal-after-references",
        OK,
        environ=(("STATE_ROOT", "/srv/data/state"), ("OWNER_BIN", "/home/operator/bin")),
    ),
    Layer("environment-empty-for-an-optional-key", OK, environ=(("OPTIONAL", ""),)),
    Layer("environment-outside-the-catalog", OK, environ=(("HOME", "/root"), ("OTHER", "x"))),
    # --- refused: the whole file ---
    Layer("too-large-env", NO, env=_long(ENV, "x", MAX_BYTES + 1)),
    Layer("too-large-default", NO, default=_long(DEFAULTS, "x", MAX_BYTES + 1)),
    Layer("too-large-example", NO, example=_long(EXAMPLE, "x", MAX_BYTES + 1)),
    Layer("not-utf8-env", NO, env=ENV.encode() + b"# \xff\n"),
    Layer("not-utf8-default", NO, default=b"A=1\n# \xc3\x28\n"),
    Layer("not-utf8-example", NO, example=b"# \xed\xa0\x80\n"),
    Layer("byte-order-mark-env", NO, env="\ufeff" + ENV),
    Layer("byte-order-mark-example", NO, example="\ufeff" + EXAMPLE),
    Layer(
        "carriage-return-on-line-one", NO, env="OWNER_USER=operator\r\nLAN_ADDRESS=192.0.2.10\r\n"
    ),
    Layer("carriage-return-on-line-two", NO, env="OWNER_USER=operator\nLAN_ADDRESS=192.0.2.10\r\n"),
    Layer("carriage-return-alone", NO, env=ENV + "\r"),
    Layer("carriage-return-in-a-comment", NO, default="# a\rb\n" + DEFAULTS),
    Layer("no-final-newline-env", NO, env=ENV.removesuffix("\n")),
    Layer("no-final-newline-default", NO, default=DEFAULTS + "LAST=1"),
    Layer("no-final-newline-example", NO, example="#"),
    # --- refused: one line ---
    Layer("no-equals-sign", NO, env=ENV + "DATA_ROOT\n"),
    Layer("semicolon-comment", NO, env=ENV + ";DATA_ROOT=/x\n"),
    Layer("spaces-only", NO, env=ENV + "   \n"),
    Layer("tab-only", NO, env=ENV + "\t\n"),
    Layer("key-in-lower-case", NO, env=ENV + "data_root=/x\n"),
    Layer("leading-space", NO, env=ENV + " DATA_ROOT=/x\n"),
    Layer("leading-tab", NO, env=ENV + "\tDATA_ROOT=/x\n"),
    Layer("export", NO, env=ENV + "export DATA_ROOT=/x\n"),
    Layer("space-before-the-equals-sign", NO, env=ENV + "DATA_ROOT =/x\n"),
    Layer("key-starts-with-a-digit", NO, default=DEFAULTS + "1KEY=x\n"),
    Layer("key-starts-with-an-underscore", NO, default=DEFAULTS + "_KEY=x\n"),
    Layer("key-with-a-hyphen", NO, default=DEFAULTS + "KEY-NAME=x\n"),
    Layer("key-outside-ascii", NO, default=DEFAULTS + "K\u00c9Y=x\n"),
    Layer("key-of-65-characters", NO, default=DEFAULTS + "K" + "_" * 64 + "=1\n"),
    Layer("empty-key", NO, default=DEFAULTS + "=value\n"),
    Layer("double-quotes", NO, env=ENV + 'DATA_ROOT="/x"\n'),
    Layer("single-quotes", NO, env=ENV + "DATA_ROOT='/x'\n"),
    Layer("backslash", NO, env=ENV + "DATA_ROOT=/a\\b\n"),
    Layer("number-sign-in-a-value", NO, env=ENV + "DATA_ROOT=/x #note\n"),
    Layer("space-after-the-equals-sign", NO, env=ENV + "DATA_ROOT= /x\n"),
    Layer("space-at-the-end", NO, env=ENV + "DATA_ROOT=/x \n"),
    Layer("tab-in-a-value", NO, env=ENV + "DATA_ROOT=/a\tb\n"),
    Layer("value-outside-ascii", NO, env=ENV + "DATA_ROOT=/caf\u00e9\n"),
    Layer("delete-character", NO, env=ENV + "DATA_ROOT=/a\x7fb\n"),
    Layer("dollar-with-no-brace", NO, default=DEFAULTS + "X=$DATA_ROOT\n"),
    Layer("reference-in-lower-case", NO, default=DEFAULTS + "X=${data_root}\n"),
    Layer("home-with-no-key", NO, default=DEFAULTS + "X=${home:}\n"),
    Layer("home-in-upper-case", NO, default=DEFAULTS + "X=${HOME:OWNER_USER}\n"),
    Layer("dollar-at-the-end", NO, default=DEFAULTS + "X=/a$\n"),
    Layer("reference-not-closed", NO, default=DEFAULTS + "X=${DATA_ROOT\n"),
    Layer("two-dollar-signs", NO, default=DEFAULTS + "X=$${DATA_ROOT}\n"),
    Layer("empty-value-in-env", NO, env=ENV + "DATA_ROOT=\n"),
    Layer("empty-value-in-example", NO, example="#OWNER_USER=\n#LAN_ADDRESS=192.0.2.10\n"),
    Layer("duplicate-key-in-default", NO, default=DEFAULTS + "DATA_ROOT=/again\n"),
    Layer("duplicate-key-in-env", NO, env=ENV + "OWNER_USER=again\n"),
    Layer("duplicate-key-in-example", NO, example=EXAMPLE + "#OWNER_USER=again\n"),
    Layer("required-key-with-a-default", NO, example=EXAMPLE + "#DATA_ROOT=/srv/data\n"),
    Layer("active-line-in-example", NO, example="OWNER_USER=operator\n"),
    Layer("example-key-in-lower-case", NO, example=EXAMPLE + "#owner=x\n"),
    Layer("example-word-with-no-space", NO, example=EXAMPLE + "#Note\n"),
    Layer("example-tab-after-the-sign", NO, example=EXAMPLE + "#\tnote\n"),
    Layer("reference-in-env", NO, env=ENV + "DATA_ROOT=${LAN_ADDRESS}\n"),
    Layer("dollar-in-env", NO, env=ENV + "DATA_ROOT=/a$b\n"),
    Layer("reference-in-example", NO, example="#OWNER_USER=${LAN_ADDRESS}\n"),
    Layer(
        "default-is-read-before-env",
        NO,
        default=DEFAULTS + "DATA_ROOT=/again\n",
        env=ENV + "data_root=/x\n",
    ),
    # --- refused: the layers ---
    Layer("unknown-key", NO, env=ENV + "SURPLUS=1\n"),
    Layer("unknown-key-before-a-required-key", NO, env="SURPLUS=1\n"),
    Layer("required-key-unset", NO, env="OWNER_USER=operator\n"),
    Layer("each-required-key-unset", NO, env=""),
    Layer("a-copy-of-example-as-env", NO, env=EXAMPLE),
    Layer("unknown-reference", NO, default=DEFAULTS + "X=${NOT_IN_THE_CATALOG}\n"),
    Layer("unknown-reference-of-a-home", NO, default=DEFAULTS + "X=${home:NOT_IN_THE_CATALOG}\n"),
    Layer(
        "unknown-reference-down-a-chain", NO, default=DEFAULTS + "A=${B}\nB=${NOT_IN_THE_CATALOG}\n"
    ),
    Layer("unset-reference", NO, default=DEFAULTS + "X=${OPTIONAL}\n"),
    Layer("unset-reference-of-a-home", NO, default=DEFAULTS + "X=${home:OPTIONAL}\n"),
    Layer("self-reference", NO, default=DEFAULTS + "X=${X}\n"),
    Layer("cycle-of-two", NO, default=DEFAULTS + "X=${Y}\nY=${X}\n"),
    Layer("cycle-below-a-key", NO, default=DEFAULTS + "A=${B}\nB=${C}\nC=${B}\n"),
    Layer("chain-of-nine", NO, default=_chain(9), example="", env=""),
    Layer("chain-of-nine-in-reverse", NO, default=_reversed_chain(9), example="", env=""),
    Layer("cycle-longer-than-a-chain", NO, default=_cycle(10), example="", env=""),
    Layer("unknown-account", NO, homes=()),
    Layer("home-with-a-space-at-the-end", NO, homes=(("operator", "/home/operator "),)),
    Layer("home-with-a-number-sign", NO, homes=(("operator", "/home/#operator"),)),
    Layer("home-with-a-dollar", NO, homes=(("operator", "/home/${DATA_ROOT}"),)),
    Layer("empty-home", NO, homes=(("operator", ""),)),
    Layer(
        "home-of-a-key-that-holds-a-reference",
        NO,
        default=DEFAULTS + "ALIAS=${OWNER_USER}\nALIAS_HOME=${home:ALIAS}\n",
    ),
    Layer(
        "value-larger-than-a-file",
        NO,
        default=DEFAULTS + "B=" + "b" * 1000 + "\nA=" + "${B}" * 70 + "\n",
    ),
    Layer(
        "value-larger-after-its-last-reference",
        NO,
        default=DEFAULTS + "B=" + "b" * 1000 + "\nA=" + "${B}" * 65 + "t" * 600 + "\n",
    ),
    Layer("environment-differs-from-a-default", NO, environ=(("DATA_ROOT", "/other"),)),
    Layer("environment-differs-from-env", NO, environ=(("OWNER_USER", "other"),)),
    Layer("environment-sets-an-optional-key", NO, environ=(("OPTIONAL", "on"),)),
    Layer("environment-differs-after-references", NO, environ=(("STATE_ROOT", "/srv/data"),)),
    Layer(
        "environment-differs-twice",
        NO,
        environ=(("LAN_ADDRESS", "192.0.2.11"), ("DATA_ROOT", "/other")),
    ),
)


def _form(body: Body) -> dict[str, Json]:
    """The input form of the bytes of one file."""
    if isinstance(body, str):
        return text_input(body)

    if isinstance(body, bytes):
        return bytes_input(body)

    return repeat_input(body)


def _file(name: str, body: Body) -> dict[str, Json]:
    """The input form of one file, with its name."""
    return {"name": name, **_form(body)}


def _bytes(body: Body) -> bytes:
    if isinstance(body, str):
        return body.encode("utf-8")

    if isinstance(body, bytes):
        return body

    return expand(body).encode("utf-8")


def _problems(error: EnvError) -> list[Json]:
    return [
        {"fault": str(one.fault), "file": one.file, "line": one.line, "key": one.key}
        for one in error.problems
    ]


def _checked(
    item_id: str, wanted: str, given: dict[str, Json], call: Callable[[], object]
) -> Vector:
    """The vector of one call. Another result than `wanted` stops the
    build, and so does an exception other than `EnvError`: no committed
    vector has the result `raised` (`vectors/AGENTS.md`, rule 5)."""
    outcome = attempt(call)
    if isinstance(outcome, Raised):
        if not isinstance(outcome.exc, EnvError):
            name = type(outcome.exc).__name__
            raise ValueError(f"{GROUP}: the entry point raises {name} for {item_id}")

        vector = refused(item_id, given, _problems(outcome.exc))
    else:
        vector = accepted(item_id, given, outcome)

    if vector.body["result"] != wanted:
        raise ValueError(f"{GROUP}: the entry point gives {item_id} another result")

    return vector


def _settle(item: Layer) -> dict[str, str]:
    """The call of the entry point: the pair, the layers and the environment."""
    pair = read_pair(
        Text(DEFAULT_NAME, _bytes(item.default)), Text(EXAMPLE_NAME, _bytes(item.example))
    )
    homes = dict(item.homes)
    settings = load(pair, Text(ENV_NAME, _bytes(item.env)), homes.get)
    check_environment(settings, dict(item.environ))

    return dict(settings.values)


def _layer_vector(item: Layer) -> Vector:
    given: dict[str, Json] = {
        "args": {
            "default": _file(DEFAULT_NAME, item.default),
            "example": _file(EXAMPLE_NAME, item.example),
            "env": _file(ENV_NAME, item.env),
            "homes": dict(item.homes),
            "environ": [[key, value] for key, value in item.environ],
        }
    }

    return _checked(item.id, item.wanted, given, lambda: _settle(item))


#: The inputs of `envfile.raw`: an id, the bytes of the file and the result.
RAWS: Final[tuple[tuple[str, Body, str], ...]] = (
    ("empty", "", ACCEPTED),
    ("line-end-only", "\n", ACCEPTED),
    (
        "keys-and-comments",
        "# hooks\nAPPROVAL_URL=http://127.0.0.1:1881/a\n\nTOKEN=a-b_c=\n",
        ACCEPTED,
    ),
    ("each-printable-sign", f"SIGNS=a{SIGNS}z\n", ACCEPTED),
    ("braces-with-no-dollar", "A={x}\n", ACCEPTED),
    ("largest-file", _long("A=1\n", "x", MAX_BYTES), ACCEPTED),
    ("too-large", _long("A=1\n", "x", MAX_BYTES + 1), REFUSED),
    ("not-utf8", b"A=1\n# \xff\n", REFUSED),
    ("byte-order-mark", "\ufeffA=1\n", REFUSED),
    ("carriage-return", "A=1\r\n", REFUSED),
    ("no-final-newline", "A=1", REFUSED),
    ("no-equals-sign", "A=1\nB\n", REFUSED),
    ("key-in-lower-case", "base=a/b@sha256:0\n", REFUSED),
    ("export", "export A=1\n", REFUSED),
    ("leading-space", " A=1\n", REFUSED),
    ("double-quotes", 'A="1"\n', REFUSED),
    ("space-at-the-end", "A=1 \n", REFUSED),
    ("empty-value", "A=\n", REFUSED),
    ("duplicate-key", "A=1\nA=2\n", REFUSED),
    ("reference", "A=${B}\nB=1\n", REFUSED),
)


def _raw_vector(item_id: str, body: Body, wanted: str) -> Vector:
    data = _bytes(body)

    return _checked(item_id, wanted, _form(body), lambda: raw(Text(PLAIN_NAME, data)))


#: The uids of the trust vectors.
ROOT: Final = 0
READER: Final = 1000
OTHER: Final = 1001

#: The type bits of a regular file. Each mode of the list carries them.
REGULAR: Final = 0o100000

#: The inputs of `envfile.trust`: an id, the owner, the mode, the reader and
#: the result.
TRUSTS: Final[tuple[tuple[str, int, int, int, str], ...]] = (
    ("root-reads-a-file-of-root", ROOT, 0o644, ROOT, ACCEPTED),
    ("root-reads-a-private-file-of-root", ROOT, 0o600, ROOT, ACCEPTED),
    ("an-account-reads-a-file-of-root", ROOT, 0o644, READER, ACCEPTED),
    ("an-account-reads-its-own-file", READER, 0o600, READER, ACCEPTED),
    ("others-can-read-and-run", READER, 0o755, READER, ACCEPTED),
    ("root-reads-a-file-of-an-account", READER, 0o600, ROOT, REFUSED),
    ("an-account-reads-a-file-of-another", OTHER, 0o600, READER, REFUSED),
    ("group-can-write", ROOT, 0o664, ROOT, REFUSED),
    ("other-can-write", READER, 0o602, READER, REFUSED),
    ("each-can-write", ROOT, 0o666, READER, REFUSED),
    ("owner-before-the-mode", OTHER, 0o666, READER, REFUSED),
)


def _trust_vector(item_id: str, owner: int, mode: int, reader: int, wanted: str) -> Vector:
    given: dict[str, Json] = {"args": {"owner": owner, "mode": REGULAR | mode, "reader": reader}}

    return _checked(
        item_id, wanted, given, lambda: check_trust(TRUSTED_NAME, owner, REGULAR | mode, reader)
    )


FAULT_NOTE: Final = (
    "A refused vector holds a list of faults in refusal, in the order of the reader. A fault is "
    "an object with four keys: fault, file, line and key. line 0 means the whole file, and an "
    "empty key means no key. A fault holds no value."
)


def surfaces() -> tuple[Surface, ...]:
    return (
        Surface(
            name=f"{GROUP}.layer",
            path=f"{GROUP}/layer.json",
            entry="creche_env.envfile.load",
            contract=CONTRACT,
            notes=(
                "The input is args: three files, homes and environ. A file is an object with its "
                "name and one input form of its bytes: default.env, example.env and .env.",
                "Replay: read_pair of the two files of the pair, then load with the .env and "
                "homes as the password database, then check_environment with environ. homes maps "
                "an account to its home. An account that homes does not name has no home.",
                "An accepted vector holds the settings in value: each catalog key that has a "
                "value, with the value after each layer. An optional key with no value is absent.",
                FAULT_NOTE,
            ),
            vectors=tuple(_layer_vector(item) for item in LAYERS),
        ),
        Surface(
            name=f"{GROUP}.raw",
            path=f"{GROUP}/raw.json",
            entry="creche_env.envfile.raw",
            contract=CONTRACT,
            notes=(
                f"The input is the bytes of one file, named {PLAIN_NAME}. The entry point reads it "
                "in the grammar-only mode: the rules of a .env, no catalog and no reference.",
                "An accepted vector holds each key of the file and its value in value.",
                FAULT_NOTE,
            ),
            vectors=tuple(_raw_vector(*item) for item in RAWS),
        ),
        Surface(
            name=f"{GROUP}.trust",
            path=f"{GROUP}/trust.json",
            entry="creche_env.envfile.check_trust",
            contract=CONTRACT,
            notes=(
                "The input is args: the uid that owns the file, the st_mode of the file and the "
                f"uid that reads it. A fault names the file {TRUSTED_NAME}.",
                "An accepted vector holds no value.",
                FAULT_NOTE,
            ),
            vectors=tuple(_trust_vector(*item) for item in TRUSTS),
        ),
    )
