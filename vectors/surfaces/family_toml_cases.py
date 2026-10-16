"""Written `family.toml` inputs: one TOML twin of each rule case, and the cases
of the TOML reader.

A twin has the id of its case in `family_cases.py` and holds the same values.
The TOML path reads each field in the strict mode, so a twin of a `lax-` case
holds the text or the float of its case, and the reader refuses it. A case of
`family_cases.py` that needs a form of YAML has no twin: an anchor, an alias,
a merge key, a tag, a YAML 1.1 word, a null, a key that is no text, a second
document and a top value that is no mapping.

The cases with an id that starts with `toml-` are the cases of the TOML
reader: the four refusals of the reader, the size limit, the level limit, a
form of TOML 1.1, a date-time, `inf` and `nan`, a value of a wrong kind at
each field, the order of the faults and two names of a family file in one
directory.

No case names a deployment. An address is TEST-NET-1 (RFC 5737).
"""

from __future__ import annotations

from typing import Final

from vectors.surfaces.family_cases import (
    HUGE_DIGITS,
    LONG_TOOL_NAME,
    LONG_TOOL_SERVER,
    NEST_INNER,
    PLACEHOLDER,
    PLATFORM_FAMILY,
    PLATFORM_SERVER_WITH_MERGE,
    Case,
)

#: The most bytes of a family file. The case at the limit and the case one
#: byte past it hold this number as text, so a changed limit moves a vector.
FAMILY_BYTES: Final = 65536

#: The most levels of a TOML text. The top table has level 1.
LEVELS: Final = 8


def _case(
    case_id: str, text: str, directory: str = "", files: dict[str, str] | None = None
) -> Case:
    return Case(case_id, text.encode("utf-8"), directory, files or {})


# A thin family every rule accepts, in the layout of `bin/yaml-to-toml.py`.
HEAD: Final = """\
name = "%NAME%"
kind = "thin"
description = "One written input."
model.router = "fast"
model.budget_usd_per_day = 1
"""

ATTENDED_HEAD: Final = HEAD.replace('kind = "thin"', 'kind = "attended"')
AUTONOMOUS_HEAD: Final = HEAD.replace('kind = "thin"', 'kind = "autonomous"')

#: An autonomous family with the one trigger it must have.
CRON_HEAD: Final = AUTONOMOUS_HEAD + 'triggers = [\n    { cron = "@hourly" },\n]\n'


def _head(**values: str) -> str:
    """`HEAD` with the value of some keys replaced. A key is the last part of a
    dotted key: `router` stands for `model.router`. A value is TOML text."""
    out: list[str] = []
    for line in HEAD.splitlines():
        key, _, value = line.partition(" = ")
        out.append(f"{key} = {values.get(key.rpartition('.')[2], value)}")

    return "\n".join(out) + "\n"


#: A second family that claims the webhook name `shared-hook`.
OTHER_WEBHOOK_FAMILY: Final = """\
name = "other-webhook-owner"
kind = "autonomous"
description = "A second owner of one webhook name."
model.router = "fast"
model.budget_usd_per_day = 1
triggers = [
    { webhook = "shared-hook" },
]
"""

#: A family that enqueues `%NAME%`, so its dispatch trigger can fire.
DISPATCHER_FAMILY: Final = """\
name = "dispatcher"
kind = "autonomous"
description = "Enqueues the family under test."
model.router = "fast"
model.budget_usd_per_day = 1
verbs.enqueue.targets = ["%NAME%"]
verbs.job_status = {}
triggers = [
    { cron = "@daily" },
]
"""

#: The text of a YAML family file, for the case of two names in one
#: directory.
YAML_FAMILY: Final = """\
name: toml-two-names
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
"""


def _padded(case_id: str, size: int) -> Case:
    """A valid family file of `size` bytes: `HEAD` and one comment line."""
    head = HEAD.replace(PLACEHOLDER, case_id)
    comment = "#" * (size - len(head.encode("utf-8")) - 1)

    return Case(case_id, f"{head}{comment}\n".encode())


def _nested(lists: int) -> str:
    """`HEAD` and an `egress` of `lists` lists, each inside the one before."""
    return HEAD + "egress = " + "[" * lists + '"x"' + "]" * lists + "\n"


TWINS: Final[tuple[Case, ...]] = (
    # --- the control: nothing wrong ---------------------------------------
    _case("ok-minimal", HEAD),
    _case("ok-attended", ATTENDED_HEAD),
    _case("ok-autonomous", CRON_HEAD),
    _case(
        "ok-every-field",
        """\
name = "%NAME%"
kind = "attended"
description = "A family that sets every field an attended family may set."
model.router = "agent-router"
model.budget_usd_per_day = 12.5
files = [
    { path = "/srv/agents/vault", mode = "ro" },
    { path = "/srv/agents/code/example", mode = "rw" },
]
tools.kagi = ["kagi_search_fetch", "kagi_extract"]
tools.ha-read = "all"
verbs.embed = {}
verbs.ha_call.allow = [
    { domain = "notify", service = "mobile_app_example_phone" },
    { domain = "light", service = "turn_on", entity_id = "light.example_lamp" },
]
delegates = ["vault-oracle"]
max_inflight_delegations = 3
egress = ["example.com", "example.org:8443"]
shell = true
sandbox_tools = ["read", "write", "edit", "grep", "find", "ls", "codemode"]
system_prompt = "replace"
sandbox.cpus = 4
sandbox.memory = "8g"
sandbox.max_resident_processes = 16
sandbox.image = "python"
skills = ["handoff", "grill-me"]
approval = ["ha_call", "invoke_agent", "kagi__kagi_extract", "ha-read__*"]
""",
    ),
    # --- unknown fields (contract 01 §7 rule 1) ----------------------------
    _case("unknown-top-near", HEAD + 'descripton = "a typo of a known field"\n'),
    _case("unknown-top-far", HEAD + "zzzzqqq = 1\n"),
    _case(
        "unknown-nested",
        HEAD
        + """\
model.budgt = 2
sandbox.cpu = 2
sandbox.memroy = "2g"
files = [
    { path = "/srv/agents/vault", mode = "ro", moed = "rw" },
]
""",
    ),
    _case(
        "unknown-verb",
        HEAD + "verbs.invoke_agent = {}\nverbs.embed.extra = 1\nverbs.relase = {}\n",
    ),
    _case("unknown-quiet", HEAD + 'quiet.bord = "board-lead"\nquiet.daily.cal = "x"\n'),
    _case("unknown-many", HEAD + 'zeta = 1\nalpha = 2\nshel = true\nKIND = "thin"\n'),
    # --- wrong type, at depth ----------------------------------------------
    _case(
        "wrong-type-depth",
        AUTONOMOUS_HEAD
        + """\
files = [
    { path = "/srv/agents/vault", mode = "ro" },
    { path = "/srv/agents/code", mode = ["ro"] },
]
verbs.ha_call.allow = [
    { domain = 5, service = "notify" },
]
triggers = [
    { cron = 5 },
    "webhook-name",
]
""",
    ),
    _case(
        "wrong-type-top",
        """\
name = 12
kind = ["thin"]
description.text = "nope"
model = "fast"
files = "/srv/agents/vault"
delegates = "vault-oracle"
shell = "maybe"
""",
    ),
    _case(
        "wrong-type-union",
        HEAD + 'tools.kagi = 5\ntools.mail = ["send_standup_email", 7]\ntools.ha.a = 1\n',
    ),
    # --- missing required --------------------------------------------------
    _case("missing-top", 'name = "%NAME%"\ndescription = "no kind and no model"\n'),
    _case(
        "missing-nested",
        """\
name = "%NAME%"
kind = "thin"
description = "One written input."
model.router = "fast"
files = [
    { path = "/srv/agents/vault" },
]
verbs.ha_call = {}
verbs.enqueue = {}
verbs.release = {}
""",
    ),
    _case("missing-and-unknown", 'nmae = "%NAME%"\nkind = "thin"\n'),
    # --- the texts that the lax mode of pydantic coerces --------------------
    _case(
        "lax-accepted",
        _head(
            description='"Every value here is a string or a float that the reader coerces."',
            budget_usd_per_day='"2.5"',
        )
        + 'shell = "yes"\nmax_inflight_delegations = "2"\n'
        + 'sandbox.cpus = 2.0\nsandbox.max_resident_processes = " 12 "\n',
    ),
    _case(
        "lax-refused",
        _head(budget_usd_per_day='"cheap"')
        + "shell = 2\nmax_inflight_delegations = 2.5\n"
        + 'sandbox.cpus = "two"\nsandbox.max_resident_processes = [12]\n',
    ),
    _case("lax-quoted-int", HEAD + 'sandbox.cpus = "2"\n'),
    _case("lax-bool-words", HEAD + 'shell = "on"\n'),
    _case("lax-bool-int", HEAD + "shell = 1\n"),
    _case("lax-bool-as-int", HEAD + "sandbox.cpus = true\n"),
    _case("lax-int-as-budget", _head(budget_usd_per_day="true")),
    _case("lax-int-as-string", _head(description="5")),
    # --- triggers (contract 01 §3.13) ---------------------------------------
    _case(
        "trigger-two-keys",
        AUTONOMOUS_HEAD + 'triggers = [\n    { cron = "@hourly", webhook = "hook-a" },\n]\n',
    ),
    _case("trigger-no-key", AUTONOMOUS_HEAD + "triggers = [\n    {},\n]\n"),
    _case(
        "trigger-three-keys",
        AUTONOMOUS_HEAD
        + "triggers = [\n"
        + '    { cron = "@daily", webhook = "hook-b", enqueue = true },\n'
        + '    { cron = "1 2 3" },\n]\n',
    ),
    _case(
        "trigger-dispatch",
        AUTONOMOUS_HEAD
        + """\
triggers = [
    { enqueue = false },
    { enqueue = true },
    { enqueue = true },
    { webhook = "Bad_Hook" },
]
""",
    ),
    _case("trigger-missing", AUTONOMOUS_HEAD),
    _case("trigger-empty-list", AUTONOMOUS_HEAD + "triggers = []\n"),
    _case(
        "trigger-cron-forms",
        AUTONOMOUS_HEAD
        + """\
triggers = [
    { cron = "0 9 * * 1-5" },
    { cron = "@weekly" },
    { cron = "@yearly" },
    { cron = "0 9 * *" },
    { cron = "0  9\\t* * 0" },
    { cron = "" },
]
""",
    ),
    # A field holds ASCII digits and `*`, `,`, `-`, `/`. U+0669 and U+00B2 are
    # digits to `str.isdigit`.
    _case(
        "trigger-cron-characters",
        AUTONOMOUS_HEAD
        + """\
triggers = [
    { cron = "0 \u0669 * * *" },
    { cron = "*/\u00b2 * * * *" },
    { cron = "0 9 * * mon" },
    { cron = "*/15 0-6,22 1 1,7 1-5" },
    { cron = "? ? ? ? ?" },
]
""",
    ),
    _case(
        "trigger-webhook-ok", AUTONOMOUS_HEAD + 'triggers = [\n    { webhook = "new-ticket" },\n]\n'
    ),
    _case(
        "trigger-webhook-shared",
        AUTONOMOUS_HEAD + 'triggers = [\n    { webhook = "shared-hook" },\n]\n',
        files={"families/other-webhook-owner/family.toml": OTHER_WEBHOOK_FAMILY},
    ),
    _case(
        "trigger-dispatch-unused", AUTONOMOUS_HEAD + "triggers = [\n    { enqueue = true },\n]\n"
    ),
    _case(
        "trigger-dispatch-used",
        AUTONOMOUS_HEAD + "triggers = [\n    { enqueue = true },\n]\n",
        files={"families/dispatcher/family.toml": DISPATCHER_FAMILY},
    ),
    # --- duplicate keys: the TOML reader refuses the text --------------------
    _case(
        "dup-key-top",
        'name = "%NAME%"\nkind = "attended"\nkind = "thin"\ndescription = "x"\n'
        'model.router = "fast"\nmodel.budget_usd_per_day = 1\n',
    ),
    _case(
        "dup-key-nested",
        'name = "%NAME%"\nkind = "thin"\ndescription = "x"\n\n[model]\nrouter = "fast"\n'
        'router = "agent-router"\nbudget_usd_per_day = 1\n',
    ),
    # --- the document as a whole (contract 01 §1 rules 3 and 4) ---
    _case("doc-empty", ""),
    _case("doc-comment-only", "# nothing but a comment\n"),
    Case("doc-not-utf8", b'name = "doc-not-utf8"\nkind = "thin"\ndescription = "\xff\xfe"\n'),
    Case("doc-utf16", HEAD.replace(PLACEHOLDER, "doc-utf16").encode("utf-16")),
    # --- identity (contract 01 §3.1) ---
    _case("rule-name-pattern", _head(name='"Bad_Name"')),
    _case(
        "rule-name-probe",
        'name = "gate-probe"\nkind = "bogus"\ndescription = ""\n'
        'model.router = "fast"\nmodel.budget_usd_per_day = 1\n',
    ),
    _case("rule-name-directory", _head(name='"another-name"')),
    _case("rule-name-newline", _head(name='"%NAME%\\n"')),
    _case("rule-kind-case", _head(kind='"Thin"')),
    _case("rule-description-long", _head(description='"' + "x" * 201 + '"')),
    _case("rule-description-max", _head(description='"' + "x" * 200 + '"')),
    _case("rule-description-blank", _head(description='"   "')),
    _case("rule-description-collapse", _head(description='"' + "x   " * 100 + '"')),
    # --- model (contract 01 §3.2) ---
    _case("rule-model", _head(router='"Agent-*"', budget_usd_per_day="1000")),
    _case("rule-model-alias", _head(router='"Agent-Router"', budget_usd_per_day="0")),
    _case("rule-budget-float", _head(budget_usd_per_day="1e+22")),
    _case("rule-budget-max", _head(budget_usd_per_day="500")),
    _case("rule-budget-over", _head(budget_usd_per_day="500.01")),
    _case("rule-budget-negative", _head(budget_usd_per_day="-1")),
    _case("rule-budget-tiny", _head(budget_usd_per_day="1e-09")),
    _case("rule-router-path", _head(router='"a/b.c-d_e"')),
    # --- files (contract 01 §3.3, §5.4, §5.5) ---
    _case(
        "rule-files",
        HEAD
        + """\
files = [
    { path = "/srv/agents/vault", mode = "ro" },
    { path = "/srv/agents/vault", mode = "rx" },
    { path = "/srv/agents/vault/*", mode = "ro" },
    { path = "srv/agents/vault", mode = "ro" },
    { path = "/srv/agents/../etc", mode = "ro" },
    { path = "/srv/agents//vault", mode = "ro" },
    { path = "/srv/agents/vault/$HOME", mode = "ro" },
    { path = "/srv/agents/sessions/chat", mode = "rw" },
    { path = "/etc", mode = "ro" },
    { path = "/srv/agents/work/platform", mode = "ro" },
    { path = "/srv/agents/work/platform-x", mode = "ro" },
]
""",
    ),
    _case(
        "rule-files-shapes",
        HEAD
        + """\
files = [
    { path = "/srv/agents/vault/a?", mode = "ro" },
    { path = "/srv/agents/vault/[ab]", mode = "ro" },
    { path = "/srv/agents/vault/.", mode = "ro" },
    { path = "/srv/agents/vault/", mode = "ro" },
    { path = "/srv/agents/vault/a b", mode = "ro" },
    { path = "/srv/agents/vault-other", mode = "ro" },
    { path = "/srv/agents/sessions", mode = "ro" },
    { path = "/srv/agents/code/agent-control", mode = "ro" },
    { path = "/", mode = "ro" },
    { path = "", mode = "ro" },
    { path = "/srv/agents/vault/x", mode = "RO" },
]
""",
    ),
    _case(
        "fence-other-mount",
        ATTENDED_HEAD
        + "files = [\n"
        + '    { path = "/srv/agents/work/platform", mode = "rw" },\n'
        + '    { path = "/srv/agents/vault", mode = "ro" },\n]\n',
        directory=PLATFORM_FAMILY,
    ),
    _case(
        "fence-mirror-ro",
        ATTENDED_HEAD
        + "files = [\n"
        + '    { path = "/srv/agents/work/platform", mode = "rw" },\n'
        + '    { path = "/srv/agents/code/agent-control", mode = "ro" },\n'
        + '    { path = "/srv/agents/code/agent-mcp", mode = "ro" },\n]\n',
        directory=PLATFORM_FAMILY,
    ),
    _case(
        "fence-mirror-rw",
        ATTENDED_HEAD
        + "files = [\n"
        + '    { path = "/srv/agents/code/agent-registry", mode = "rw" },\n'
        + '    { path = "/srv/agents/code/agent-control/src", mode = "ro" },\n]\n',
        directory=PLATFORM_FAMILY,
    ),
    _case(
        "fence-release-inside",
        ATTENDED_HEAD + 'verbs.release.components = ["chaperone", "registry-data"]\n',
        directory=PLATFORM_FAMILY,
    ),
    _case(
        "fence-withheld-named",
        ATTENDED_HEAD + 'tools.github-platform = ["get_file_contents", "merge_pull_request"]\n',
    ),
    _case(
        "fence-withheld-all",
        ATTENDED_HEAD + 'tools.github-platform = "all"\n',
        files={"mcp/github-platform/server.yaml": PLATFORM_SERVER_WITH_MERGE},
    ),
    # --- tools (contract 01 §3.4) ---
    _case(
        "rule-tools",
        HEAD
        + """\
tools.Bad_Server = ["x"]
tools.no-such-server = ["x"]
tools.kagi = ["kagi_extract", "kagi_extract", "bad tool", "no_such_tool"]
tools.mail = []
tools.ha = "some"
tools.ha-read = "all"
""",
    ),
    _case("rule-tools-all-auto", CRON_HEAD + 'tools.kagi = "all"\n'),
    _case("rule-tools-all-upper", HEAD + 'tools.kagi = "ALL"\n'),
    # --- verbs (contract 01 §3.5) ---
    _case(
        "rule-verbs",
        HEAD
        + """\
verbs.ha_call.allow = [
    { domain = "NOTIFY", service = "X", entity_id = "bad" },
]
verbs.enqueue.targets = ["no-such-family", "chat", "scrum-lead"]
verbs.release.components = ["chaperone", "not-a-component"]
""",
    ),
    _case(
        "rule-verbs-empty",
        HEAD
        + "verbs.ha_call.allow = []\nverbs.enqueue.targets = []\n"
        + "verbs.release.components = []\n",
    ),
    _case("rule-job-status-alone", HEAD + "verbs.job_status = {}\n"),
    _case(
        "rule-verbs-ok",
        HEAD
        + "verbs.embed = {}\n"
        + 'verbs.enqueue.targets = ["scrum-lead"]\n'
        + "verbs.job_status = {}\n"
        + 'verbs.ha_call.allow = [{ domain = "light", service = "turn_off", '
        + 'entity_id = "light.a_1" }]\n',
    ),
    # --- delegates (contract 01 §3.6) ---
    _case("rule-delegates-thin", HEAD + 'delegates = ["vault-oracle"]\n'),
    _case(
        "rule-delegates",
        ATTENDED_HEAD
        + 'delegates = ["vault-oracle", "vault-oracle", "%NAME%", "no-such-family", "chat"]\n',
    ),
    _case("rule-inflight-range", HEAD + "max_inflight_delegations = 9\n"),
    _case("rule-inflight-zero", HEAD + "max_inflight_delegations = 0\n"),
    _case("rule-inflight-unused", HEAD + "max_inflight_delegations = 4\n"),
    _case(
        "rule-inflight-used",
        ATTENDED_HEAD + 'delegates = ["vault-oracle"]\nmax_inflight_delegations = 8\n',
    ),
    # --- egress (contract 01 §3.7) ---
    _case(
        "rule-egress",
        HEAD
        + 'egress = ["192.0.2.10", "*.example.com", "example.com:99999", "example.com:x", '
        + '"-bad-.example", "::1", "ok.example:443", ""]\n',
    ),
    _case(
        "rule-egress-edges",
        HEAD
        + 'egress = ["example.com:0", "example.com:65535", "example.com:65536", "example.com:", '
        + '"example.com.", "EXAMPLE.com", "a_b.example", "192.0.2.10:443", "999.999.999.999", '
        + '"1.2.3", "localhost", "example.com:\u0661", "example.com:+1", "https://example.com"]\n',
    ),
    # U+00B2 is a digit to `str.isdigit` and no number to `int`.
    _case("rule-egress-superscript", HEAD + 'egress = ["example.com:\u00b2"]\n'),
    _case(
        "rule-egress-port-digits",
        HEAD
        + 'egress = ["example.com:00443", "example.com:000443", "example.com:0000000000", '
        + '"example.com:065536", "example.com:100000", "example.com:'
        + "9" * HUGE_DIGITS
        + '"]\n',
    ),
    # --- the runtime fields (contract 01 §3.8) ---
    _case(
        "rule-runtime",
        HEAD + 'sandbox_tools = ["read", "bash", "teleport", "read"]\nsystem_prompt = "prepend"\n',
    ),
    _case("rule-runtime-empty", HEAD + "sandbox_tools = []\n"),
    # --- sandbox (contract 01 §3.9) ---
    _case(
        "rule-sandbox",
        HEAD
        + 'sandbox.cpus = 99\nsandbox.memory = "64g"\nsandbox.max_resident_processes = 0\n'
        + 'sandbox.image = "registry.example/x/y"\n',
    ),
    _case(
        "rule-sandbox-memory",
        HEAD + 'sandbox.memory = "2x"\nsandbox.max_resident_processes = 33\n',
    ),
    _case(
        "rule-sandbox-carry",
        HEAD + 'sandbox.memory = "256m"\nsandbox.max_resident_processes = 12\n',
    ),
    _case("rule-sandbox-small", HEAD + 'sandbox.memory = "255m"\nsandbox.cpus = 0\n'),
    _case(
        "rule-sandbox-max",
        HEAD + 'sandbox.memory = "16g"\nsandbox.cpus = 8\nsandbox.max_resident_processes = 32\n',
    ),
    _case("rule-sandbox-memory-forms", HEAD + 'sandbox.memory = "02g"\n'),
    _case("rule-sandbox-memory-upper", HEAD + 'sandbox.memory = "2G"\n'),
    _case("rule-sandbox-memory-int", HEAD + "sandbox.memory = 2048\n"),
    # --- skills (contract 01 §3.10) ---
    _case("rule-skills", HEAD + 'skills = ["handoff", "handoff", "Bad_Skill", "no-such-skill"]\n'),
    # --- approval (contract 01 §3.11) ---
    _case(
        "rule-approval",
        ATTENDED_HEAD
        + """\
tools.kagi = ["kagi_extract"]
tools.ha-read = "all"
verbs.embed = {}
approval = [
    "embed",
    "embed",
    "ha_call",
    "invoke_agent",
    "mail__send_standup_email",
    "kagi__kagi_search_fetch",
    "kagi__kagi_extract",
    "kagi__*",
    "ha-read__*",
    "ha-read__ha_get_state",
    "*",
    "kagi__",
    "__kagi_extract",
]
""",
    ),
    # --- the kind-fenced fields (contract 01 §3.12 to §3.14) ---
    _case(
        "rule-kind-job",
        ATTENDED_HEAD + 'job.timeout = "30s"\ntriggers = []\nmax_running_turns = 2\n',
    ),
    _case("rule-job-timeout", HEAD + 'job.timeout = "5h"\n'),
    _case("rule-job-timeout-warn", HEAD + 'job.timeout = "3m"\n'),
    _case("rule-job-timeout-shape", HEAD + 'job.timeout = "soon"\n'),
    _case("rule-job-timeout-max", HEAD + 'job.timeout = "1h"\n'),
    _case("rule-job-timeout-zero", HEAD + 'job.timeout = "0s"\n'),
    _case("rule-job-default", HEAD + "job = {}\n"),
    _case("rule-running-turns", CRON_HEAD + "max_running_turns = 9\n"),
    _case("rule-running-turns-ok", CRON_HEAD + "max_running_turns = 8\n"),
    _case("rule-running-turns-zero", CRON_HEAD + "max_running_turns = 0\n"),
    # --- quiet (contract 01 §3.15) ---
    _case("rule-quiet-thin", HEAD + "quiet = {}\n"),
    _case(
        "rule-quiet-no-cron",
        AUTONOMOUS_HEAD + 'triggers = [\n    { webhook = "a-hook" },\n]\nquiet = {}\n',
    ),
    _case("rule-quiet-jobs", CRON_HEAD + 'verbs.enqueue.targets = ["scrum-lead"]\nquiet = {}\n'),
    _case("rule-quiet-board", CRON_HEAD + 'tools.kagi = ["kagi_extract"]\nquiet.board = "kagi"\n'),
    _case("rule-quiet-board-none", CRON_HEAD + 'quiet.board = "board-lead"\n'),
    _case(
        "rule-quiet-daily",
        CRON_HEAD
        + """\
quiet.daily.call = "mail__send_standup_email"
quiet.daily.hour = 24
quiet.daily.zone = "Nowhere/Nothing"
quiet.floor_hours = 169
""",
    ),
    _case(
        "rule-quiet-zones",
        CRON_HEAD
        + 'verbs.embed = {}\nquiet.daily.call = "embed"\nquiet.daily.hour = 0\n'
        + 'quiet.daily.zone = ""\n',
    ),
    _case(
        "rule-quiet-zone-path",
        CRON_HEAD
        + 'verbs.embed = {}\nquiet.daily.call = "embed"\nquiet.daily.hour = -1\n'
        + 'quiet.daily.zone = "../etc/passwd"\nquiet.floor_hours = 0\n',
    ),
    _case(
        "rule-quiet-ok",
        CRON_HEAD
        + """\
tools.board-lead = ["survey_board"]
tools.mail = ["send_standup_email"]
quiet.board = "board-lead"
quiet.daily.call = "mail__send_standup_email"
quiet.daily.hour = 23
quiet.daily.zone = "Etc/UTC"
quiet.floor_hours = 168
""",
    ),
    # --- more texts that the lax mode of pydantic reads as a number ----------
    _case(
        "lax-int-forms",
        HEAD + 'sandbox.cpus = "0-1"\nsandbox.max_resident_processes = "0_1_2.00"\n',
    ),
    _case(
        "lax-int-plus",
        HEAD + 'sandbox.cpus = "+02"\nsandbox.max_resident_processes = " 1_2 "\n',
    ),
    _case(
        "lax-int-refused",
        HEAD + 'sandbox.cpus = "2."\nsandbox.max_resident_processes = "1__2"\n',
    ),
    _case(
        "lax-int-sign-twice",
        HEAD + 'sandbox.cpus = "-0-1"\nsandbox.max_resident_processes = "+-1"\n',
    ),
    _case(
        "lax-int-float-edge",
        HEAD + "sandbox.cpus = 9.3e+18\nsandbox.max_resident_processes = 2.5\n",
    ),
    _case(
        "lax-int-not-finite",
        HEAD + "sandbox.cpus = inf\nsandbox.max_resident_processes = nan\n",
    ),
    _case("lax-float-forms", _head(budget_usd_per_day='"1_0.5e+0"')),
    _case("lax-float-space-underscore", _head(budget_usd_per_day='" 1_0"')),
    _case("lax-float-word", _head(budget_usd_per_day='"Infinity"')),
    # An integer outside 64 bits: the TOML reader refuses the text.
    _case("lax-float-large-int", _head(budget_usd_per_day="1" + "0" * 400)),
    _case("lax-bool-forms", HEAD + 'shell = "T"\n'),
    _case("lax-bool-space", HEAD + 'shell = " yes"\n'),
    _case("lax-bool-float", HEAD + "shell = 1.0\n"),
    _case("lax-bool-float-other", HEAD + "shell = 0.5\n"),
    _case("lax-bool-large-int", HEAD + "shell = 99999999999999999999999\n"),
    # --- an unknown field near more than one known field ---
    _case("unknown-tie", HEAD + "skils = []\nshel = true\ntool = {}\n"),
    _case("unknown-long", HEAD + "x" * 250 + " = 1\n"),
    # --- one more edge of a rule ---
    _case("rule-sandbox-memory-over", HEAD + 'sandbox.memory = "16385m"\n'),
    _case(
        "rule-sandbox-memory-digits",
        HEAD + 'sandbox.memory = "' + "9" * HUGE_DIGITS + 'g"\n',
    ),
    _case("rule-job-timeout-digits", HEAD + 'job.timeout = "' + "9" * HUGE_DIGITS + 's"\n'),
    _case(
        "rule-approval-all-undeclared",
        ATTENDED_HEAD
        + 'tools.kagi = "all"\napproval = ["kagi__no_such_tool", "kagi__kagi_extract"]\n',
    ),
    _case(
        "fence-withheld-all-auto",
        CRON_HEAD + 'tools.github-platform = "all"\n',
        files={"mcp/github-platform/server.yaml": PLATFORM_SERVER_WITH_MERGE},
    ),
    _case(
        "rule-skills-no-file",
        HEAD + 'skills = ["no-file-skill"]\n',
        files={"skills/no-file-skill/notes.txt": "A skill directory with no skill file.\n"},
    ),
    # --- collections inside collections: the TOML reader refuses each text ---
    _case("nest-128-levels", HEAD + "egress = " + "[" * NEST_INNER + "]" * NEST_INNER + "\n"),
    _case(
        "nest-129-levels",
        HEAD + "egress = " + "[" * (NEST_INNER + 1) + "]" * (NEST_INNER + 1) + "\n",
    ),
    # --- a tool name past the limit (contract 01 §3.4) ---
    _case(
        "long-tool-name",
        HEAD + f'tools.long-tool-server = ["{LONG_TOOL_NAME}"]\n',
        files={"mcp/long-tool-server/server.yaml": LONG_TOOL_SERVER},
    ),
)

#: The cases of the TOML reader and of the loader of a TOML file.
READER_CASES: Final[tuple[Case, ...]] = (
    # --- the four refusals of the reader, and the size limit ---------------
    _padded("toml-size-at-limit", FAMILY_BYTES),
    _padded("toml-size-past-limit", FAMILY_BYTES + 1),
    _case("toml-byte-order-mark", "\ufeff" + HEAD),
    # `doc-not-utf8` holds a byte that is no UTF-8, and `dup-key-top` holds a
    # key two times.
    # --- TOML 1.1: a comma before the end of an inline table ---------------
    _case(
        "toml-1-1-comma",
        'name = "%NAME%"\nkind = "thin"\ndescription = "One written input."\n'
        'model = { router = "fast", budget_usd_per_day = 1, }\n',
    ),
    # --- the level limit -----------------------------------------------------
    _case("toml-levels-at-limit", _nested(LEVELS - 1)),
    _case("toml-levels-past-limit", _nested(LEVELS)),
    # --- tables, a list of tables and an inline table ------------------------
    _case(
        "toml-table-headers",
        """\
name = "%NAME%"
kind = "attended"
description = "One written input."
model = { router = "fast", budget_usd_per_day = 2.5 }
shell = true

[sandbox]
cpus = 4
memory = "4g"

[[files]]
path = "/srv/agents/vault"
mode = "ro"

[[files]]
path = "/srv/agents/code/example"
mode = "rw"

[tools]
kagi = ["kagi_extract"]

[verbs.embed]
""",
    ),
    # --- a date-time: no field takes one ------------------------------------
    _case("toml-date-text", _head(description="2026-10-03")),
    _case("toml-date-integer", HEAD + "max_inflight_delegations = 1979-05-27T07:32:00Z\n"),
    _case(
        "toml-date-model",
        'name = "%NAME%"\nkind = "thin"\ndescription = "One written input."\n'
        "model = 1979-05-27T07:32:00\n",
    ),
    _case("toml-time-list", HEAD + "egress = [07:32:00]\n"),
    # --- a float field --------------------------------------------------------
    _case("toml-budget-inf", _head(budget_usd_per_day="inf")),
    _case("toml-budget-nan", _head(budget_usd_per_day="nan")),
    _case("toml-budget-integer", _head(budget_usd_per_day="12")),
    # --- an integer field ------------------------------------------------------
    _case("toml-integer-text", HEAD + 'max_inflight_delegations = "2"\n'),
    _case("toml-integer-float", HEAD + "max_inflight_delegations = 2.0\n"),
    _case("toml-integer-boolean", HEAD + "max_inflight_delegations = true\n"),
    # --- a value of a wrong kind at each field, at each nested table and at
    # each leaf. A wrong kind at a table hides each fault below it, so each
    # depth has a case of its own. ----------------------------------------------
    _case(
        "toml-wrong-type-each-field",
        HEAD
        + """\
files = "x"
tools = ["kagi"]
verbs = 1
delegates = "x"
max_inflight_delegations = 1.5
egress = { x = 1 }
shell = "x"
sandbox_tools = 1
system_prompt = []
sandbox = [1]
skills = true
approval = 1.5
job = "x"
triggers = { cron = "@daily" }
max_running_turns = "1"
quiet = 1
""",
    ),
    _case(
        "toml-wrong-type-each-block",
        HEAD
        + """\
files = [1]
verbs.embed = 1
verbs.ha_call = "x"
verbs.enqueue = []
verbs.job_status = true
verbs.release = 1.5
triggers = [
    { cron = "@daily" },
    "x",
]
quiet.daily = 1
""",
    ),
    _case(
        "toml-wrong-type-each-leaf",
        _head(router="1")
        + """\
files = [
    { path = 1, mode = true },
]
verbs.ha_call.allow = [
    { domain = 1, service = 1, entity_id = 1 },
    1,
]
verbs.enqueue.targets = "x"
verbs.release.components = [1]
delegates = [1]
sandbox_tools = [true]
sandbox.cpus = 1.5
sandbox.memory = 4
sandbox.max_resident_processes = "1"
sandbox.image = 1
skills = [[]]
approval = [1.5]
job.timeout = 1
triggers = [
    { cron = 1, webhook = 1, enqueue = "yes" },
]
max_running_turns = 1.5
quiet.board = 1
quiet.daily.call = 1
quiet.daily.hour = "1"
quiet.daily.zone = 1
quiet.daily.weekdays_only = "yes"
quiet.floor_hours = "1"
""",
    ),
    # --- the order of the faults: the reader sorts the keys of a table -------
    _case("toml-unknown-reverse", HEAD + "zeta = 1\nalpha = 2\n"),
    _case("toml-tools-reverse", HEAD + "tools.zz = 5\ntools.bb = 7\n"),
    # --- two names of a family file in one directory --------------------------
    _case(
        "toml-two-names",
        HEAD,
        files={"families/toml-two-names/family.yaml": YAML_FAMILY},
    ),
)

CASES: Final[tuple[Case, ...]] = (*TWINS, *READER_CASES)
