"""Written `family.yaml` inputs: one per rule, per shape error and per YAML trap.

Every case is YAML TEXT, not a dict dumped to YAML. Half of them are about
how the text is read. `%NAME%` stands for the directory the case is filed
under, so `name` matches its directory unless the case is about that rule.

No case names a deployment. An address is TEST-NET-1 (RFC 5737).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

#: Stands for the case's directory. Not `@`: a cron shorthand starts with one.
PLACEHOLDER: Final = "%NAME%"

#: The family the platform fence allows (contract 01 §5.5).
PLATFORM_FAMILY: Final = "agent-control"


@dataclass(frozen=True)
class Case:
    """One written input, and where it sits in the registry."""

    id: str
    body: bytes
    #: The directory under `families/`. Empty means the case's own id.
    directory: str = ""
    #: Other registry files the case needs, by path relative to the registry.
    files: dict[str, str] = field(default_factory=dict[str, str])

    def directory_name(self) -> str:
        return self.directory or self.id

    def text_bytes(self) -> bytes:
        return self.body.replace(PLACEHOLDER.encode(), self.directory_name().encode())


def _case(
    case_id: str, text: str, directory: str = "", files: dict[str, str] | None = None
) -> Case:
    return Case(case_id, text.encode("utf-8"), directory, files or {})


# A thin family every rule accepts. A case appends to it or replaces a line.
HEAD: Final = """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
"""

ATTENDED_HEAD: Final = HEAD.replace("kind: thin", "kind: attended")
AUTONOMOUS_HEAD: Final = HEAD.replace("kind: thin", "kind: autonomous")

#: An autonomous family with the one trigger it must have.
CRON_HEAD: Final = AUTONOMOUS_HEAD + 'triggers:\n  - cron: "@hourly"\n'


def _head(**lines: str) -> str:
    """`HEAD` with whole lines replaced, by their key."""
    out: list[str] = []
    for line in HEAD.splitlines():
        key = line.partition(":")[0]
        out.append(lines.get(key, line))

    return "\n".join(out) + "\n"


#: A platform server file that declares the tool no family is granted
#: (contract 01 §5.5 rule 7). The fixture's own file leaves it out.
PLATFORM_SERVER_WITH_MERGE: Final = """\
name: github-platform
identity: "A written server file that declares the withheld tool."

install:
  source: pypi
  package: example-mcp
  version: 1.0.0
  lock: mcp/github-platform/install.lock
  python: "3.12"

run:
  entrypoint: example-mcp
  env: {}

tools:
  - name: get_file_contents
    description: Read one file at a ref.
  - name: merge_pull_request
    description: Merge a pull request.
"""

#: A second family that claims the webhook name `shared-hook`.
OTHER_WEBHOOK_FAMILY: Final = """\
name: other-webhook-owner
kind: autonomous
description: A second owner of one webhook name.
model: { router: fast, budget_usd_per_day: 1 }
triggers:
  - webhook: shared-hook
"""

#: A family that enqueues `%NAME%`, so its dispatch trigger can fire.
DISPATCHER_FAMILY: Final = """\
name: dispatcher
kind: autonomous
description: Enqueues the family under test.
model: { router: fast, budget_usd_per_day: 1 }
verbs:
  enqueue: { targets: [%NAME%] }
  job_status: {}
triggers:
  - cron: "@daily"
"""

#: A tool name of 65 characters. `agent_family` has no limit for a tool name.
LONG_TOOL_NAME: Final = "a" * 65

#: A server file that declares the tool `LONG_TOOL_NAME`.
LONG_TOOL_SERVER: Final = f"""\
name: long-tool-server
identity: One written server.
install: {{ source: agent-mcp }}
run: {{ entrypoint: example-mcp }}
tools:
  - {{ name: {LONG_TOOL_NAME}, description: One tool. }}
"""

#: The count of collections around the innermost one, for the two nesting
#: cases. The mapping at the top is one level more.
NEST_INNER: Final = 127

#: A count of flow sequences that no supported Python version reads.
_DEEP_FLOW: Final = 10_000

#: A count of decimal digits past the limit of 4300 digits of Python.
_HUGE_DIGITS: Final = 5_000

#: A count of base 16 digits whose number has more than 4300 decimal digits.
_HUGE_HEX_DIGITS: Final = 4_000

#: A count of base 60 parts whose number is past the largest float.
_HUGE_PARTS: Final = 200

CASES: Final[tuple[Case, ...]] = (
    # --- the control: nothing wrong ---------------------------------------
    _case("ok-minimal", HEAD),
    _case("ok-attended", ATTENDED_HEAD),
    _case("ok-autonomous", CRON_HEAD),
    _case(
        "ok-every-field",
        """\
name: %NAME%
kind: attended
description: A family that sets every field an attended family may set.
model: { router: agent-router, budget_usd_per_day: 12.5 }
files:
  - { path: /srv/agents/vault, mode: ro }
  - { path: /srv/agents/code/example, mode: rw }
tools:
  kagi: [kagi_search_fetch, kagi_extract]
  ha-read: all
verbs:
  embed: {}
  ha_call:
    allow:
      - { domain: notify, service: mobile_app_example_phone }
      - { domain: light, service: turn_on, entity_id: light.example_lamp }
delegates: [vault-oracle]
max_inflight_delegations: 3
egress: [example.com, "example.org:8443"]
shell: true
sandbox_tools: [read, write, edit, grep, find, ls, codemode]
system_prompt: replace
sandbox: { cpus: 4, memory: 8g, max_resident_processes: 16, image: python }
skills: [handoff, grill-me]
approval: [ha_call, invoke_agent, kagi__kagi_extract, "ha-read__*"]
""",
    ),
    # --- unknown fields (contract 01 §7 rule 1) ----------------------------
    _case("unknown-top-near", HEAD + "descripton: a typo of a known field\n"),
    _case("unknown-top-far", HEAD + "zzzzqqq: 1\n"),
    _case(
        "unknown-nested",
        """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1, budgt: 2 }
sandbox: { cpu: 2, memroy: 2g }
files:
  - { path: /srv/agents/vault, mode: ro, moed: rw }
""",
    ),
    _case("unknown-verb", HEAD + "verbs: { invoke_agent: {}, embed: { extra: 1 }, relase: {} }\n"),
    _case("unknown-quiet", HEAD + "quiet: { bord: board-lead, daily: { cal: x } }\n"),
    _case("unknown-many", HEAD + "zeta: 1\nalpha: 2\nshel: true\nKIND: thin\n"),
    # --- wrong type, at depth ----------------------------------------------
    _case(
        "wrong-type-depth",
        """\
name: %NAME%
kind: autonomous
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
files:
  - { path: /srv/agents/vault, mode: ro }
  - { path: /srv/agents/code, mode: [ro] }
verbs:
  ha_call:
    allow:
      - { domain: 5, service: notify }
triggers:
  - cron: 5
  - webhook-name
""",
    ),
    _case(
        "wrong-type-top",
        """\
name: 12
kind: [thin]
description: { text: nope }
model: fast
files: /srv/agents/vault
delegates: vault-oracle
shell: maybe
""",
    ),
    _case(
        "wrong-type-union",
        HEAD + "tools:\n  kagi: 5\n  mail: [send_standup_email, 7]\n  ha: { a: 1 }\n",
    ),
    _case("wrong-type-keys", HEAD + "tools:\n  5: [a]\n12: twelve\n"),
    _case(
        "wrong-type-model-key",
        "name: %NAME%\nkind: thin\ndescription: x\n"
        "model: { router: fast, budget_usd_per_day: 1, 7: x }\n",
    ),
    # --- missing required --------------------------------------------------
    _case("missing-top", "name: %NAME%\ndescription: no kind and no model\n"),
    _case(
        "missing-nested",
        """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast }
files:
  - { path: /srv/agents/vault }
verbs: { ha_call: {}, enqueue: {}, release: {} }
""",
    ),
    _case("missing-and-unknown", "nmae: %NAME%\nkind: thin\n"),
    # --- pydantic's lax coercions -------------------------------------------
    _case(
        "lax-accepted",
        """\
name: %NAME%
kind: thin
description: Every value here is a string or a float that the reader coerces.
model: { router: fast, budget_usd_per_day: "2.5" }
shell: "yes"
max_inflight_delegations: "2"
sandbox: { cpus: 2.0, max_resident_processes: " 12 " }
""",
    ),
    _case(
        "lax-refused",
        """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: "cheap" }
shell: 2
max_inflight_delegations: 2.5
sandbox: { cpus: "two", max_resident_processes: [12] }
""",
    ),
    _case("lax-quoted-int", HEAD + 'sandbox: { cpus: "2" }\n'),
    _case("lax-bool-words", HEAD + 'shell: "on"\n'),
    _case("lax-bool-int", HEAD + "shell: 1\n"),
    _case("lax-bool-as-int", HEAD + "sandbox: { cpus: true }\n"),
    _case("lax-int-as-budget", _head(model="model: { router: fast, budget_usd_per_day: true }")),
    _case("lax-int-as-string", _head(description="description: 5")),
    # --- triggers (contract 01 §3.13) ---------------------------------------
    _case(
        "trigger-two-keys",
        AUTONOMOUS_HEAD + 'triggers:\n  - { cron: "@hourly", webhook: hook-a }\n',
    ),
    _case("trigger-no-key", AUTONOMOUS_HEAD + "triggers:\n  - {}\n"),
    _case(
        "trigger-three-keys",
        AUTONOMOUS_HEAD
        + 'triggers:\n  - { cron: "@daily", webhook: hook-b, enqueue: true }\n  - cron: "1 2 3"\n',
    ),
    _case(
        "trigger-dispatch",
        AUTONOMOUS_HEAD
        + "triggers:\n  - enqueue: false\n  - enqueue: true\n  - enqueue: true\n"
        + "  - webhook: Bad_Hook\n",
    ),
    _case("trigger-missing", AUTONOMOUS_HEAD),
    _case("trigger-empty-list", AUTONOMOUS_HEAD + "triggers: []\n"),
    _case(
        "trigger-cron-forms",
        AUTONOMOUS_HEAD
        + 'triggers:\n  - cron: "0 9 * * 1-5"\n  - cron: "@weekly"\n  - cron: "@yearly"\n'
        + '  - cron: "0 9 * *"\n  - cron: "0  9\\t* * 0"\n  - cron: ""\n',
    ),
    # A field holds ASCII digits and `*`, `,`, `-`, `/`. U+0669 and U+00B2 are
    # digits to `str.isdigit`.
    _case(
        "trigger-cron-characters",
        AUTONOMOUS_HEAD
        + 'triggers:\n  - cron: "0 \u0669 * * *"\n  - cron: "*/\u00b2 * * * *"\n'
        + '  - cron: "0 9 * * mon"\n  - cron: "*/15 0-6,22 1 1,7 1-5"\n  - cron: "? ? ? ? ?"\n',
    ),
    _case("trigger-webhook-ok", AUTONOMOUS_HEAD + "triggers:\n  - webhook: new-ticket\n"),
    _case(
        "trigger-webhook-shared",
        AUTONOMOUS_HEAD + "triggers:\n  - webhook: shared-hook\n",
        files={"families/other-webhook-owner/family.yaml": OTHER_WEBHOOK_FAMILY},
    ),
    _case("trigger-dispatch-unused", AUTONOMOUS_HEAD + "triggers:\n  - enqueue: true\n"),
    _case(
        "trigger-dispatch-used",
        AUTONOMOUS_HEAD + "triggers:\n  - enqueue: true\n",
        files={"families/dispatcher/family.yaml": DISPATCHER_FAMILY},
    ),
    # --- duplicate keys ---
    _case(
        "dup-key-top",
        "name: %NAME%\nkind: attended\nkind: thin\ndescription: x\n"
        "model: { router: fast, budget_usd_per_day: 1 }\n",
    ),
    _case(
        "dup-key-nested",
        "name: %NAME%\nkind: thin\ndescription: x\nmodel:\n  router: fast\n"
        "  router: agent-router\n  budget_usd_per_day: 1\n",
    ),
    # --- YAML 1.1 against YAML 1.2 ---
    _case("yaml-yes-bool-field", HEAD + "shell: yes\n"),
    _case("yaml-no-bool-field", HEAD + "shell: no\n"),
    _case("yaml-yes-str-field", _head(description="description: yes")),
    _case("yaml-no-str-field", _head(model="model: { router: no, budget_usd_per_day: 1 }")),
    _case("yaml-on-off", HEAD + "shell: on\negress: [off, NO]\n"),
    _case("yaml-off-bool-field", HEAD + "shell: off\n"),
    _case("yaml-y-single", _head(description="description: y")),
    _case("yaml-true-forms", HEAD + "shell: True\n"),
    _case("yaml-octal-new", HEAD + "sandbox: { cpus: 0o10 }\n"),
    _case("yaml-octal-old", HEAD + "sandbox: { cpus: 010 }\n"),
    _case("yaml-octal-old-small", HEAD + "sandbox: { cpus: 04 }\n"),
    _case("yaml-exp-float", _head(model="model: { router: fast, budget_usd_per_day: 1e2 }")),
    _case("yaml-exp-1e3", _head(model="model: { router: fast, budget_usd_per_day: 1e3 }")),
    _case("yaml-exp-dotted", _head(model="model: { router: fast, budget_usd_per_day: 1.0e+2 }")),
    _case("yaml-exp-int", HEAD + "sandbox: { cpus: 1e0 }\n"),
    _case("yaml-tilde", HEAD + "job: ~\ntriggers: ~\nsandbox: ~\n"),
    _case("yaml-tilde-str", _head(description="description: ~")),
    _case("yaml-null-word", _head(description="description: null")),
    _case("yaml-timestamp", _head(description="description: 2026-10-03")),
    _case("yaml-timestamp-quoted", _head(description='description: "2026-10-03"')),
    _case("yaml-sexagesimal", HEAD + "job: { timeout: 1:30 }\n"),
    _case("yaml-sexagesimal-int", HEAD + "sandbox: { cpus: 0:2 }\n"),
    _case("yaml-underscore-int", HEAD + "sandbox: { cpus: 1_0 }\n"),
    _case("yaml-hex-int", HEAD + "sandbox: { cpus: 0x4 }\n"),
    _case("yaml-binary-int", HEAD + "sandbox: { cpus: 0b100 }\n"),
    _case("yaml-plus-int", HEAD + "sandbox: { cpus: +4 }\n"),
    _case("yaml-inf", _head(model="model: { router: fast, budget_usd_per_day: .inf }")),
    _case("yaml-nan", _head(model="model: { router: fast, budget_usd_per_day: .nan }")),
    _case("yaml-float-str-field", _head(description="description: 1.0")),
    _case("yaml-empty-value", HEAD + "files:\ndelegates:\n"),
    _case(
        "yaml-merge-key",
        """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
files:
  - &vault { path: /srv/agents/vault, mode: ro }
  - { <<: *vault, path: /srv/agents/code }
""",
    ),
    _case(
        "yaml-anchor-alias",
        HEAD + "skills: &list [handoff]\ndelegates: *list\n",
    ),
    _case(
        "yaml-tabs",
        "name: %NAME%\nkind: thin\ndescription: x\nmodel:\n\trouter: fast\n"
        "\tbudget_usd_per_day: 1\n",
    ),
    _case("yaml-unclosed-flow", HEAD + "files: [ { path: /srv/agents/vault, mode: ro }\n"),
    _case("yaml-bad-indent", "name: %NAME%\n  kind: thin\ndescription: x\n"),
    _case("yaml-big-int", HEAD + "max_inflight_delegations: 99999999999999999999999\n"),
    _case("yaml-huge-int", HEAD + "max_inflight_delegations: " + "9" * _HUGE_DIGITS + "\n"),
    _case("yaml-tag-str", HEAD + "sandbox: { cpus: !!str 2 }\n"),
    _case("yaml-tag-int", HEAD + 'sandbox: { cpus: !!int "2" }\n'),
    _case("yaml-tag-float", HEAD + "sandbox: { cpus: !!float 2 }\n"),
    _case("yaml-tag-python", HEAD + "shell: !!python/name:os.system\n"),
    _case("yaml-tag-local", HEAD + "shell: !secret true\n"),
    _case("yaml-tag-binary", _head(description="description: !!binary aGVsbG8=")),
    # One member only. A set of two has no fixed order: the list that the
    # reader makes from it follows the hash seed of the process.
    _case("yaml-tag-set", HEAD + "skills: !!set { handoff }\n"),
    _case("yaml-directive-1-1", "%YAML 1.1\n---\n" + HEAD),
    _case("yaml-directive-1-2", "%YAML 1.2\n---\n" + HEAD + "shell: yes\n"),
    _case("yaml-directive-2-0", "%YAML 2.0\n---\n" + HEAD),
    _case("yaml-crlf", HEAD.replace("\n", "\r\n")),
    _case("yaml-bom", "\ufeff" + HEAD),
    _case("yaml-nul", HEAD + "shell: false\x00\n"),
    _case("yaml-c1-control", _head(description='description: "a\x85b"')),
    _case("yaml-escape-surrogate", _head(description='description: "\\ud800"')),
    _case("yaml-escape-nul", _head(description='description: "a\\0b"')),
    _case("yaml-deep-flow", HEAD + "skills: " + "[" * _DEEP_FLOW + "]" * _DEEP_FLOW + "\n"),
    _case("yaml-multiline-str", _head(description="description: >\n  folded\n  text\n")),
    _case("yaml-doc-end", HEAD + "...\n"),
    _case("yaml-doc-start", "---\n" + HEAD),
    _case("yaml-comment-eol", _head(kind="kind: thin # the smallest kind")),
    _case("yaml-quoted-key", HEAD.replace("kind:", '"kind":')),
    _case("yaml-key-int", HEAD + "tools:\n  12: [a]\n"),
    _case("yaml-key-bool", HEAD + "tools:\n  yes: [a]\n"),
    _case("yaml-key-null", HEAD + "tools:\n  ~: [a]\n"),
    _case("yaml-complex-key", HEAD + "tools:\n  ? [a, b]\n  : [c]\n"),
    # --- the document as a whole (contract 01 §1 rules 3 and 4) ---
    _case("doc-two", HEAD + "---\nname: second\n"),
    _case("doc-empty", ""),
    _case("doc-comment-only", "# nothing but a comment\n"),
    _case("doc-top-list", "- name: %NAME%\n- kind: thin\n"),
    _case("doc-top-scalar", "just a string\n"),
    _case("doc-top-null", "~\n"),
    _case("doc-top-int", "5\n"),
    Case("doc-not-utf8", b"name: doc-not-utf8\nkind: thin\ndescription: \xff\xfe\n"),
    Case("doc-utf16", HEAD.replace(PLACEHOLDER, "doc-utf16").encode("utf-16")),
    # --- identity (contract 01 §3.1) ---
    _case("rule-name-pattern", _head(name="name: Bad_Name")),
    _case(
        "rule-name-probe",
        "name: gate-probe\nkind: bogus\ndescription: ''\n"
        "model: { router: fast, budget_usd_per_day: 1 }\n",
    ),
    _case("rule-name-directory", _head(name="name: another-name")),
    _case("rule-name-newline", _head(name='name: "%NAME%\\n"')),
    _case("rule-kind-case", _head(kind="kind: Thin")),
    _case("rule-description-long", _head(description="description: " + "x" * 201)),
    _case("rule-description-max", _head(description="description: " + "x" * 200)),
    _case("rule-description-blank", _head(description='description: "   "')),
    _case(
        "rule-description-collapse",
        _head(description='description: "' + "x   " * 100 + '"'),
    ),
    # --- model (contract 01 §3.2) ---
    _case("rule-model", _head(model="model: { router: 'Agent-*', budget_usd_per_day: 1000 }")),
    _case(
        "rule-model-alias", _head(model="model: { router: Agent-Router, budget_usd_per_day: 0 }")
    ),
    _case("rule-budget-float", _head(model="model: { router: fast, budget_usd_per_day: 1.0e+22 }")),
    _case("rule-budget-max", _head(model="model: { router: fast, budget_usd_per_day: 500 }")),
    _case("rule-budget-over", _head(model="model: { router: fast, budget_usd_per_day: 500.01 }")),
    _case("rule-budget-negative", _head(model="model: { router: fast, budget_usd_per_day: -1 }")),
    _case("rule-budget-tiny", _head(model="model: { router: fast, budget_usd_per_day: 1.0e-9 }")),
    _case("rule-router-path", _head(model="model: { router: a/b.c-d_e, budget_usd_per_day: 1 }")),
    # --- files (contract 01 §3.3, §5.4, §5.5) ---
    _case(
        "rule-files",
        """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
files:
  - { path: /srv/agents/vault, mode: ro }
  - { path: /srv/agents/vault, mode: rx }
  - { path: "/srv/agents/vault/*", mode: ro }
  - { path: srv/agents/vault, mode: ro }
  - { path: /srv/agents/../etc, mode: ro }
  - { path: /srv/agents//vault, mode: ro }
  - { path: /srv/agents/vault/$HOME, mode: ro }
  - { path: /srv/agents/sessions/chat, mode: rw }
  - { path: /etc, mode: ro }
  - { path: /srv/agents/work/platform, mode: ro }
  - { path: /srv/agents/work/platform-x, mode: ro }
""",
    ),
    _case(
        "rule-files-shapes",
        """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
files:
  - { path: "/srv/agents/vault/a?", mode: ro }
  - { path: "/srv/agents/vault/[ab]", mode: ro }
  - { path: /srv/agents/vault/., mode: ro }
  - { path: /srv/agents/vault/, mode: ro }
  - { path: "/srv/agents/vault/a b", mode: ro }
  - { path: /srv/agents/vault-other, mode: ro }
  - { path: /srv/agents/sessions, mode: ro }
  - { path: /srv/agents/code/agent-control, mode: ro }
  - { path: /, mode: ro }
  - { path: "", mode: ro }
  - { path: /srv/agents/vault/x, mode: RO }
""",
    ),
    _case(
        "fence-other-mount",
        ATTENDED_HEAD
        + "files:\n  - { path: /srv/agents/work/platform, mode: rw }\n"
        + "  - { path: /srv/agents/vault, mode: ro }\n",
        directory=PLATFORM_FAMILY,
    ),
    _case(
        "fence-mirror-ro",
        ATTENDED_HEAD
        + "files:\n  - { path: /srv/agents/work/platform, mode: rw }\n"
        + "  - { path: /srv/agents/code/agent-control, mode: ro }\n"
        + "  - { path: /srv/agents/code/agent-mcp, mode: ro }\n",
        directory=PLATFORM_FAMILY,
    ),
    _case(
        "fence-mirror-rw",
        ATTENDED_HEAD
        + "files:\n  - { path: /srv/agents/code/agent-registry, mode: rw }\n"
        + "  - { path: /srv/agents/code/agent-control/src, mode: ro }\n",
        directory=PLATFORM_FAMILY,
    ),
    _case(
        "fence-release-inside",
        ATTENDED_HEAD + "verbs:\n  release: { components: [chaperone, registry-data] }\n",
        directory=PLATFORM_FAMILY,
    ),
    _case(
        "fence-withheld-named",
        ATTENDED_HEAD + "tools:\n  github-platform: [get_file_contents, merge_pull_request]\n",
    ),
    _case(
        "fence-withheld-all",
        ATTENDED_HEAD + "tools:\n  github-platform: all\n",
        files={"mcp/github-platform/server.yaml": PLATFORM_SERVER_WITH_MERGE},
    ),
    # --- tools (contract 01 §3.4) ---
    _case(
        "rule-tools",
        """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
tools:
  Bad_Server: [x]
  no-such-server: [x]
  kagi: [kagi_extract, kagi_extract, "bad tool", no_such_tool]
  mail: []
  ha: some
  ha-read: all
""",
    ),
    _case("rule-tools-all-auto", CRON_HEAD + "tools:\n  kagi: all\n"),
    _case("rule-tools-all-upper", HEAD + "tools:\n  kagi: ALL\n"),
    # --- verbs (contract 01 §3.5) ---
    _case(
        "rule-verbs",
        """\
name: %NAME%
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
verbs:
  ha_call:
    allow:
      - { domain: NOTIFY, service: X, entity_id: bad }
  enqueue: { targets: [no-such-family, chat, scrum-lead] }
  release: { components: [chaperone, not-a-component] }
""",
    ),
    _case(
        "rule-verbs-empty",
        HEAD
        + "verbs:\n  ha_call: { allow: [] }\n  enqueue: { targets: [] }\n"
        + "  release: { components: [] }\n",
    ),
    _case("rule-job-status-alone", HEAD + "verbs: { job_status: {} }\n"),
    _case(
        "rule-verbs-ok",
        HEAD
        + "verbs:\n  embed: {}\n  enqueue: { targets: [scrum-lead] }\n  job_status: {}\n"
        + "  ha_call: { allow: [ { domain: light, service: turn_off, entity_id: light.a_1 } ] }\n",
    ),
    _case("rule-verbs-null", HEAD + "verbs: { embed: ~, job_status: ~ }\n"),
    # --- delegates (contract 01 §3.6) ---
    _case("rule-delegates-thin", HEAD + "delegates: [vault-oracle]\n"),
    _case(
        "rule-delegates",
        ATTENDED_HEAD + "delegates: [vault-oracle, vault-oracle, %NAME%, no-such-family, chat]\n",
    ),
    _case("rule-inflight-range", HEAD + "max_inflight_delegations: 9\n"),
    _case("rule-inflight-zero", HEAD + "max_inflight_delegations: 0\n"),
    _case("rule-inflight-unused", HEAD + "max_inflight_delegations: 4\n"),
    _case(
        "rule-inflight-used",
        ATTENDED_HEAD + "delegates: [vault-oracle]\nmax_inflight_delegations: 8\n",
    ),
    # --- egress (contract 01 §3.7) ---
    _case(
        "rule-egress",
        HEAD
        + "egress: [192.0.2.10, '*.example.com', 'example.com:99999', 'example.com:x', "
        + "'-bad-.example', '::1', 'ok.example:443', '']\n",
    ),
    _case(
        "rule-egress-edges",
        HEAD
        + "egress: ['example.com:0', 'example.com:65535', 'example.com:65536', 'example.com:', "
        + "'example.com.', 'EXAMPLE.com', 'a_b.example', '192.0.2.10:443', '999.999.999.999', "
        + "'1.2.3', 'localhost', 'example.com:\u0661', 'example.com:+1', 'https://example.com']\n",
    ),
    # U+00B2 is a digit to `str.isdigit` and no number to `int`.
    _case("rule-egress-superscript", HEAD + "egress: ['example.com:\u00b2']\n"),
    _case(
        "rule-egress-port-digits",
        HEAD
        + "egress: ['example.com:00443', 'example.com:0000000000', 'example.com:065536', "
        + "'example.com:100000', 'example.com:"
        + "9" * _HUGE_DIGITS
        + "']\n",
    ),
    # --- the runtime fields (contract 01 §3.8) ---
    _case(
        "rule-runtime",
        HEAD + "sandbox_tools: [read, bash, teleport, read]\nsystem_prompt: prepend\n",
    ),
    _case("rule-runtime-empty", HEAD + "sandbox_tools: []\n"),
    # --- sandbox (contract 01 §3.9) ---
    _case(
        "rule-sandbox",
        HEAD
        + "sandbox: { cpus: 99, memory: 64g, max_resident_processes: 0, "
        + "image: registry.example/x/y }\n",
    ),
    _case("rule-sandbox-memory", HEAD + "sandbox: { memory: 2x, max_resident_processes: 33 }\n"),
    _case("rule-sandbox-carry", HEAD + "sandbox: { memory: 256m, max_resident_processes: 12 }\n"),
    _case("rule-sandbox-small", HEAD + "sandbox: { memory: 255m, cpus: 0 }\n"),
    _case(
        "rule-sandbox-max", HEAD + "sandbox: { memory: 16g, cpus: 8, max_resident_processes: 32 }\n"
    ),
    _case("rule-sandbox-memory-forms", HEAD + "sandbox: { memory: 02g }\n"),
    _case("rule-sandbox-memory-upper", HEAD + "sandbox: { memory: 2G }\n"),
    _case("rule-sandbox-memory-int", HEAD + "sandbox: { memory: 2048 }\n"),
    # --- skills (contract 01 §3.10) ---
    _case("rule-skills", HEAD + "skills: [handoff, handoff, Bad_Skill, no-such-skill]\n"),
    # --- approval (contract 01 §3.11) ---
    _case(
        "rule-approval",
        """\
name: %NAME%
kind: attended
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
tools:
  kagi: [kagi_extract]
  ha-read: all
verbs:
  embed: {}
approval:
  - embed
  - embed
  - ha_call
  - invoke_agent
  - mail__send_standup_email
  - kagi__kagi_search_fetch
  - kagi__kagi_extract
  - "kagi__*"
  - "ha-read__*"
  - ha-read__ha_get_state
  - "*"
  - kagi__
  - __kagi_extract
""",
    ),
    # --- the kind-fenced fields (contract 01 §3.12 to §3.14) ---
    _case(
        "rule-kind-job",
        ATTENDED_HEAD + "job: { timeout: 30s }\ntriggers: []\nmax_running_turns: 2\n",
    ),
    _case("rule-job-timeout", HEAD + "job: { timeout: 5h }\n"),
    _case("rule-job-timeout-warn", HEAD + "job: { timeout: 3m }\n"),
    _case("rule-job-timeout-shape", HEAD + "job: { timeout: soon }\n"),
    _case("rule-job-timeout-max", HEAD + "job: { timeout: 1h }\n"),
    _case("rule-job-timeout-zero", HEAD + "job: { timeout: 0s }\n"),
    _case("rule-job-default", HEAD + "job: {}\n"),
    _case("rule-running-turns", CRON_HEAD + "max_running_turns: 9\n"),
    _case("rule-running-turns-ok", CRON_HEAD + "max_running_turns: 8\n"),
    _case("rule-running-turns-zero", CRON_HEAD + "max_running_turns: 0\n"),
    # --- quiet (contract 01 §3.15) ---
    _case("rule-quiet-thin", HEAD + "quiet: {}\n"),
    _case(
        "rule-quiet-no-cron",
        AUTONOMOUS_HEAD + "triggers:\n  - webhook: a-hook\nquiet: {}\n",
    ),
    _case(
        "rule-quiet-jobs",
        CRON_HEAD + "verbs:\n  enqueue: { targets: [scrum-lead] }\nquiet: {}\n",
    ),
    _case(
        "rule-quiet-board",
        CRON_HEAD + "tools:\n  kagi: [kagi_extract]\nquiet: { board: kagi }\n",
    ),
    _case("rule-quiet-board-none", CRON_HEAD + "quiet: { board: board-lead }\n"),
    _case(
        "rule-quiet-daily",
        CRON_HEAD
        + "quiet:\n  daily: { call: mail__send_standup_email, hour: 24, zone: Nowhere/Nothing }\n"
        + "  floor_hours: 169\n",
    ),
    _case(
        "rule-quiet-zones",
        CRON_HEAD + "verbs: { embed: {} }\nquiet:\n  daily: { call: embed, hour: 0, zone: '' }\n",
    ),
    _case(
        "rule-quiet-zone-path",
        CRON_HEAD
        + "verbs: { embed: {} }\nquiet:\n  daily: { call: embed, hour: -1, zone: ../etc/passwd }\n"
        + "  floor_hours: 0\n",
    ),
    _case(
        "rule-quiet-ok",
        CRON_HEAD
        + "tools:\n  board-lead: [survey_board]\n  mail: [send_standup_email]\n"
        + "quiet:\n  board: board-lead\n"
        + "  daily: { call: mail__send_standup_email, hour: 23, zone: Etc/UTC }\n"
        + "  floor_hours: 168\n",
    ),
    # --- more of how pydantic reads a text as a number or a boolean ---
    _case("lax-int-forms", HEAD + 'sandbox: { cpus: "0-1", max_resident_processes: "0_1_2.00" }\n'),
    _case("lax-int-plus", HEAD + 'sandbox: { cpus: "+02", max_resident_processes: " 1_2 " }\n'),
    _case("lax-int-refused", HEAD + 'sandbox: { cpus: "2.", max_resident_processes: "1__2" }\n'),
    _case(
        "lax-int-sign-twice", HEAD + 'sandbox: { cpus: "-0-1", max_resident_processes: "+-1" }\n'
    ),
    _case("lax-int-float-edge", HEAD + "sandbox: { cpus: 9.3e+18, max_resident_processes: 2.5 }\n"),
    _case("lax-int-not-finite", HEAD + "sandbox: { cpus: .inf, max_resident_processes: .nan }\n"),
    _case(
        "lax-float-forms",
        _head(model='model: { router: fast, budget_usd_per_day: "1_0.5e+0" }'),
    ),
    _case(
        "lax-float-space-underscore",
        _head(model='model: { router: fast, budget_usd_per_day: " 1_0" }'),
    ),
    _case("lax-float-word", _head(model='model: { router: fast, budget_usd_per_day: "Infinity" }')),
    _case(
        "lax-float-large-int",
        _head(model="model: { router: fast, budget_usd_per_day: 1" + "0" * 400 + " }"),
    ),
    _case("lax-bool-forms", HEAD + 'shell: "T"\n'),
    _case("lax-bool-space", HEAD + 'shell: " yes"\n'),
    _case("lax-bool-float", HEAD + "shell: 1.0\n"),
    _case("lax-bool-float-other", HEAD + "shell: 0.5\n"),
    _case("lax-bool-large-int", HEAD + "shell: 99999999999999999999999\n"),
    _case("lax-binary-int", HEAD + "sandbox: { cpus: !!binary Mg== }\n"),
    _case("lax-binary-not-utf8", _head(description="description: !!binary /w==")),
    _case("lax-binary-key", HEAD + "tools:\n  !!binary a2FnaQ==: all\n"),
    _case("lax-binary-model-key", HEAD + "!!binary c2hlbGw=: true\n"),
    # --- more of what a YAML 1.1 reader gives ---
    _case("yaml-merge-key-form", HEAD + "base: &base { shell: true }\n<<: *base\n"),
    _case(
        "yaml-merge-list",
        "first: &first { name: %NAME%, kind: thin }\n"
        "second: &second { kind: attended, description: Merged. }\n"
        "<<: [*first, *second]\nmodel: { router: fast, budget_usd_per_day: 1 }\n",
    ),
    _case("yaml-merge-scalar", HEAD + "<<: 5\n"),
    _case("yaml-anchor-alias-form", HEAD + "delegates: &list [vault-oracle]\napproval: *list\n"),
    _case("yaml-alias-undefined", HEAD + "delegates: *nowhere\n"),
    _case("yaml-anchor-twice", HEAD + "egress: [&a x.example, &a y.example]\n"),
    _case("yaml-value-key", HEAD + "shell: =\n"),
    _case(
        "yaml-sexagesimal-float", _head(model="model: { router: fast, budget_usd_per_day: 1:30.5 }")
    ),
    _case("yaml-float-forms", _head(model="model: { router: fast, budget_usd_per_day: 1_0.2_5 }")),
    _case("yaml-key-float", HEAD + "tools:\n  1.5: [a]\n.inf: 1\n"),
    _case("yaml-key-date", HEAD + "2026-10-03: 1\n2026-10-03 10:20:30.5 +01:30: 2\n"),
    _case("yaml-key-large-int", HEAD + "99999999999999999999999: 1\n-5: 2\n"),
    _case("yaml-key-same-number", HEAD + "tools:\n  1: [a]\n  1.0: [b]\n  true: 5\n"),
    _case("yaml-tag-pairs", HEAD + "skills: !!pairs [ a: b ]\negress: !!omap [ c: d ]\n"),
    _case("yaml-tag-set-model", HEAD + "sandbox: !!set { cpus }\n"),
    _case("yaml-tag-non-specific", HEAD + "sandbox: { cpus: ! 2 }\n"),
    _case("yaml-tag-verbatim", HEAD + "sandbox: { cpus: !<tag:yaml.org,2002:int> 2 }\n"),
    _case("yaml-tag-handle", "%TAG !y! tag:yaml.org,2002:\n---\n" + HEAD + "shell: !y!str yes\n"),
    _case("yaml-tag-handle-unknown", HEAD + "shell: !y!str yes\n"),
    _case("yaml-tag-binary-bad", _head(description="description: !!binary a")),
    _case("yaml-tag-seq-on-scalar", HEAD + "skills: !!seq handoff\n"),
    _case("yaml-block-scalars", _head(description="description: |+\n  kept\n\n")),
    _case("yaml-block-indicator", _head(description="description: |0\n  text")),
    _case("yaml-escape-unknown", _head(description='description: "a\\qb"')),
    _case("yaml-escape-forms", _head(description='description: "\\x41\\u00e9\\U0001F600\\N\\_"')),
    _case("yaml-quote-open", _head(description="description: 'open")),
    _case("yaml-tab-indent", HEAD + "sandbox:\n\tcpus: 2\n"),
    _case("yaml-block-in-flow", HEAD + "egress: [ - a ]\n"),
    _case("yaml-doc-top-float", "1.5\n"),
    _case("yaml-doc-top-bool", "yes\n"),
    _case("yaml-doc-top-date", "2026-10-03\n"),
    _case("yaml-doc-top-time", "2026-10-03T10:20:30Z\n"),
    _case("yaml-doc-top-binary", "!!binary aGVsbG8=\n"),
    _case("yaml-doc-top-set", "!!set { a }\n"),
    _case("yaml-line-separator", HEAD + "shell: true\u2028egress: []\n"),
    _case("yaml-long-snippet", HEAD + "egress: [" + "a" * 90 + ", @bad, " + "b" * 90 + "]\n"),
    # --- a scalar that has no value ---
    _case(
        "yaml-huge-hex-int",
        HEAD + "max_inflight_delegations: 0x" + "f" * _HUGE_HEX_DIGITS + "\n",
    ),
    _case("yaml-date-no-day", HEAD + "shell: 2001-02-30\n"),
    _case("yaml-tag-int-empty", HEAD + 'sandbox: { cpus: !!int "" }\n'),
    _case("yaml-tag-bool-word", HEAD + "shell: !!bool maybe\n"),
    _case("yaml-tag-timestamp-word", HEAD + "shell: !!timestamp soon\n"),
    _case(
        "yaml-sexagesimal-float-huge",
        HEAD + "sandbox: { cpus: 1" + ":0" * _HUGE_PARTS + ".5 }\n",
    ),
    # --- an unknown field near more than one known field ---
    _case("unknown-tie", HEAD + "skils: []\nshel: true\ntool: {}\n"),
    _case("unknown-long", HEAD + "x" * 250 + ": 1\n"),
    # --- one more edge of a rule ---
    _case("rule-sandbox-memory-over", HEAD + "sandbox: { memory: 16385m }\n"),
    _case(
        "rule-sandbox-memory-digits",
        HEAD + "sandbox: { memory: " + "9" * _HUGE_DIGITS + "g }\n",
    ),
    _case("rule-job-timeout-digits", HEAD + "job: { timeout: " + "9" * _HUGE_DIGITS + "s }\n"),
    _case(
        "rule-approval-all-undeclared",
        ATTENDED_HEAD + "tools:\n  kagi: all\napproval: [kagi__no_such_tool, kagi__kagi_extract]\n",
    ),
    _case(
        "fence-withheld-all-auto",
        CRON_HEAD + "tools:\n  github-platform: all\n",
        files={"mcp/github-platform/server.yaml": PLATFORM_SERVER_WITH_MERGE},
    ),
    _case(
        "rule-skills-no-file",
        HEAD + "skills: [no-file-skill]\n",
        files={"skills/no-file-skill/notes.txt": "A skill directory with no skill file.\n"},
    ),
    # --- collections inside collections ---
    _case("nest-128-levels", HEAD + "egress: " + "[" * NEST_INNER + "]" * NEST_INNER + "\n"),
    _case(
        "nest-129-levels",
        HEAD + "egress: " + "[" * (NEST_INNER + 1) + "]" * (NEST_INNER + 1) + "\n",
    ),
    # --- a tool name with no limit (contract 01 §3.4) ---
    _case(
        "long-tool-name",
        HEAD + f"tools:\n  long-tool-server: [{LONG_TOOL_NAME}]\n",
        files={"mcp/long-tool-server/server.yaml": LONG_TOOL_SERVER},
    ),
)
