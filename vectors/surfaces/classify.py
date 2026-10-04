"""`classify` (contract 01 §6, contract 05 §5.4): two family files to one diff.

The entry point is `agent_family.classify`. It takes two parsed family files
and reads no disk, so a vector holds the two YAML texts and nothing else.
"""

from __future__ import annotations

from typing import Final

from agent_family import Diff, classify, parse_family

from vectors.core import Json, Surface, Vector, accepted, normalize

CONTRACT: Final = "contract 01 §6"
ENTRY: Final = "agent_family.classify"

NOTES: Final = (
    "The input holds two arguments: old and new. Each one is the text of one family.yaml.",
    "To replay a vector: parse each text with parse_family, then call classify(old, new).",
    "value.changes holds each change in order. The other keys of value are what the Diff "
    "answers: changed, refused (the fields), needs_switch, switch_mode, steps and reason.",
    "Each text has the shape of a family file. A text can break a value rule: classify reads "
    "a parsed file, not a valid one.",
)

BASE: Final = """\
name: base
kind: thin
description: One written input.
model: { router: fast, budget_usd_per_day: 1 }
"""

AUTONOMOUS: Final = (
    BASE.replace("kind: thin", "kind: autonomous") + 'triggers:\n  - cron: "@hourly"\n'
)

FULL: Final = """\
name: base
kind: attended
description: One written input.
model: { router: fast, budget_usd_per_day: 2.5 }
files:
  - { path: /srv/agents/vault, mode: ro }
  - { path: /srv/agents/code/example, mode: rw }
tools:
  kagi: [kagi_search_fetch, kagi_extract]
  ha-read: all
verbs:
  embed: {}
  ha_call: { allow: [{ domain: notify, service: send }] }
delegates: [vault-oracle]
max_inflight_delegations: 3
egress: [example.invalid, "example.invalid:443"]
shell: true
sandbox_tools: [read, write]
system_prompt: append
sandbox: { cpus: 2, memory: 2g, max_resident_processes: 12, image: base }
skills: [handoff]
approval: [embed]
"""


def _swap(text: str, old: str, new: str) -> str:
    """`text` with one part replaced. A part that is absent is a fault of a case."""
    if old not in text:
        raise ValueError(f"a classify case replaces {old!r}, which its text does not hold")

    return text.replace(old, new)


#: Each case: its id, the old text and the new text.
PAIRS: Final[tuple[tuple[str, str, str], ...]] = (
    ("same", BASE, BASE),
    ("same-full", FULL, FULL),
    (
        "same-order",
        FULL,
        _swap(FULL, "[kagi_search_fetch, kagi_extract]", "[kagi_extract, kagi_search_fetch]"),
    ),
    # --- immutable fields and the description ---
    ("name", BASE, _swap(BASE, "name: base", "name: other")),
    ("kind", BASE, _swap(BASE, "kind: thin", "kind: attended")),
    ("kind-invalid", BASE, _swap(BASE, "kind: thin", "kind: Thin")),
    ("description", BASE, _swap(BASE, "One written input.", "Another text.")),
    # --- model ---
    ("router", BASE, _swap(BASE, "router: fast", "router: slow")),
    ("budget-up", BASE, _swap(BASE, "budget_usd_per_day: 1", "budget_usd_per_day: 2.5")),
    ("budget-down", FULL, _swap(FULL, "budget_usd_per_day: 2.5", "budget_usd_per_day: 1")),
    ("budget-exponent", BASE, _swap(BASE, "budget_usd_per_day: 1", "budget_usd_per_day: 1.0e+22")),
    ("budget-tiny", BASE, _swap(BASE, "budget_usd_per_day: 1", "budget_usd_per_day: 1.0e-7")),
    ("budget-inf", BASE, _swap(BASE, "budget_usd_per_day: 1", "budget_usd_per_day: .inf")),
    ("budget-nan", BASE, _swap(BASE, "budget_usd_per_day: 1", "budget_usd_per_day: .nan")),
    (
        "budget-nan-both",
        _swap(BASE, "budget_usd_per_day: 1", "budget_usd_per_day: .nan"),
        _swap(BASE, "budget_usd_per_day: 1", "budget_usd_per_day: .nan"),
    ),
    # --- tools ---
    ("tools-add-server", BASE, BASE + "tools: { kagi: [a] }\n"),
    ("tools-remove-server", BASE + "tools: { kagi: [a] }\n", BASE),
    ("tools-swap-server", BASE + "tools: { kagi: [a] }\n", BASE + "tools: { mail: [a] }\n"),
    ("tools-add-tool", BASE + "tools: { kagi: [a] }\n", BASE + "tools: { kagi: [a, b] }\n"),
    ("tools-remove-tool", BASE + "tools: { kagi: [a, b] }\n", BASE + "tools: { kagi: [a] }\n"),
    ("tools-swap-tool", BASE + "tools: { kagi: [a] }\n", BASE + "tools: { kagi: [b] }\n"),
    ("tools-to-all", BASE + "tools: { kagi: [a] }\n", BASE + "tools: { kagi: all }\n"),
    ("tools-from-all", BASE + "tools: { kagi: all }\n", BASE + "tools: { kagi: [a] }\n"),
    ("tools-all-case", BASE + "tools: { kagi: all }\n", BASE + "tools: { kagi: ALL }\n"),
    (
        "tools-two-moves",
        BASE + "tools: { kagi: [a], mail: [b, c] }\n",
        BASE + "tools: { kagi: [a, b], mail: [b] }\n",
    ),
    (
        "tools-server-and-tool",
        BASE + "tools: { kagi: [a] }\n",
        BASE + "tools: { kagi: [a, b], mail: [c] }\n",
    ),
    ("tools-twice", BASE + "tools: { kagi: [a] }\n", BASE + "tools: { kagi: [a, a] }\n"),
    # --- verbs ---
    ("verbs-add", BASE, BASE + "verbs: { embed: {} }\n"),
    ("verbs-remove", BASE + "verbs: { embed: {} }\n", BASE),
    ("verbs-swap", BASE + "verbs: { embed: {} }\n", BASE + "verbs: { job_status: {} }\n"),
    (
        "verbs-fence",
        BASE + "verbs: { enqueue: { targets: [a] } }\n",
        BASE + "verbs: { enqueue: { targets: [a, b] } }\n",
    ),
    # --- delegates, the cap and approval ---
    ("delegates-add", BASE, BASE + "delegates: [a]\n"),
    ("delegates-remove", BASE + "delegates: [a, b]\n", BASE + "delegates: [a]\n"),
    ("delegates-swap", BASE + "delegates: [a]\n", BASE + "delegates: [b]\n"),
    ("delegates-order", BASE + "delegates: [a, b]\n", BASE + "delegates: [b, a]\n"),
    ("inflight-up", BASE, BASE + "max_inflight_delegations: 4\n"),
    ("inflight-down", BASE, BASE + "max_inflight_delegations: 1\n"),
    ("inflight-large", BASE, BASE + "max_inflight_delegations: 99999999999999999999999\n"),
    ("approval-add", BASE, BASE + "approval: [embed]\n"),
    ("approval-remove", BASE + "approval: [embed]\n", BASE),
    ("approval-swap", BASE + "approval: [embed]\n", BASE + "approval: [release]\n"),
    # --- config ---
    ("egress-add", BASE, BASE + "egress: [example.invalid]\n"),
    ("egress-remove", BASE + "egress: [example.invalid]\n", BASE),
    ("skills-add", BASE, BASE + "skills: [handoff]\n"),
    ("skills-swap", BASE + "skills: [handoff]\n", BASE + "skills: [grill-me]\n"),
    ("shell-on", BASE, BASE + "shell: true\n"),
    ("shell-off", BASE + "shell: true\n", BASE),
    ("sandbox-tools-add", BASE, BASE + "sandbox_tools: [read, grep, find, ls, write]\n"),
    ("sandbox-tools-remove", BASE, BASE + "sandbox_tools: [read]\n"),
    ("sandbox-tools-order", BASE, BASE + "sandbox_tools: [ls, find, grep, read]\n"),
    ("system-prompt", BASE, BASE + "system_prompt: replace\n"),
    # --- the kind-fenced fields ---
    ("job-add", BASE, BASE + "job: {}\n"),
    ("job-remove", BASE + "job: { timeout: 30s }\n", BASE),
    ("job-up", BASE + "job: { timeout: 30s }\n", BASE + "job: { timeout: 2m }\n"),
    ("job-down", BASE + "job: { timeout: 1h }\n", BASE + "job: { timeout: 59m }\n"),
    ("job-same-time", BASE + "job: { timeout: 60s }\n", BASE + "job: { timeout: 1m }\n"),
    ("job-invalid", BASE + "job: { timeout: 30s }\n", BASE + "job: { timeout: soon }\n"),
    ("triggers-add", AUTONOMOUS, AUTONOMOUS + "  - webhook: new-ticket\n"),
    ("triggers-remove", AUTONOMOUS + "  - webhook: new-ticket\n", AUTONOMOUS),
    ("triggers-swap", AUTONOMOUS, _swap(AUTONOMOUS, '"@hourly"', '"@daily"')),
    ("triggers-none", AUTONOMOUS, BASE.replace("kind: thin", "kind: autonomous")),
    ("triggers-twice", AUTONOMOUS, AUTONOMOUS + '  - cron: "@hourly"\n'),
    (
        "triggers-order",
        AUTONOMOUS + "  - enqueue: true\n",
        _swap(AUTONOMOUS, '  - cron: "@hourly"\n', '  - enqueue: true\n  - cron: "@hourly"\n'),
    ),
    ("turns-add", AUTONOMOUS, AUTONOMOUS + "max_running_turns: 2\n"),
    (
        "turns-up",
        AUTONOMOUS + "max_running_turns: 2\n",
        AUTONOMOUS + "max_running_turns: 3\n",
    ),
    (
        "turns-down",
        AUTONOMOUS + "max_running_turns: 3\n",
        AUTONOMOUS + "max_running_turns: 2\n",
    ),
    ("quiet-add", AUTONOMOUS, AUTONOMOUS + "quiet: {}\n"),
    (
        "quiet-change",
        AUTONOMOUS + "quiet: { floor_hours: 2 }\n",
        AUTONOMOUS + "quiet: { floor_hours: 3 }\n",
    ),
    # --- the sandbox: a replacement ---
    ("files-add", BASE, BASE + "files:\n  - { path: /srv/agents/vault, mode: ro }\n"),
    ("files-remove", BASE + "files:\n  - { path: /srv/agents/vault, mode: ro }\n", BASE),
    (
        "files-to-rw",
        BASE + "files:\n  - { path: /srv/agents/vault, mode: ro }\n",
        BASE + "files:\n  - { path: /srv/agents/vault, mode: rw }\n",
    ),
    (
        "files-to-ro",
        BASE + "files:\n  - { path: /srv/agents/vault, mode: rw }\n",
        BASE + "files:\n  - { path: /srv/agents/vault, mode: ro }\n",
    ),
    (
        "files-every-move",
        FULL,
        _swap(
            _swap(FULL, "/srv/agents/code/example, mode: rw", "/srv/agents/code/other, mode: ro"),
            "/srv/agents/vault, mode: ro",
            "/srv/agents/vault, mode: rw",
        ),
    ),
    (
        "files-order",
        FULL,
        _swap(
            FULL,
            "  - { path: /srv/agents/vault, mode: ro }\n"
            "  - { path: /srv/agents/code/example, mode: rw }\n",
            "  - { path: /srv/agents/code/example, mode: rw }\n"
            "  - { path: /srv/agents/vault, mode: ro }\n",
        ),
    ),
    (
        "files-twice",
        BASE + "files:\n  - { path: /srv/agents/vault, mode: ro }\n",
        BASE
        + "files:\n  - { path: /srv/agents/vault, mode: ro }\n"
        + "  - { path: /srv/agents/vault, mode: rw }\n",
    ),
    ("image", BASE, BASE + "sandbox: { image: python }\n"),
    ("cpus-up", BASE, BASE + "sandbox: { cpus: 4 }\n"),
    ("cpus-down", BASE, BASE + "sandbox: { cpus: 1 }\n"),
    ("memory-up", BASE, BASE + "sandbox: { memory: 4g }\n"),
    ("memory-down", BASE, BASE + "sandbox: { memory: 512m }\n"),
    ("memory-same-size", BASE, BASE + "sandbox: { memory: 2048m }\n"),
    ("memory-invalid", BASE, BASE + "sandbox: { memory: 2x }\n"),
    ("resident-up", BASE, BASE + "sandbox: { max_resident_processes: 13 }\n"),
    ("resident-down", BASE, BASE + "sandbox: { max_resident_processes: 4 }\n"),
    # --- more than one field ---
    ("add-only-replace", BASE, BASE + "sandbox: { cpus: 4, memory: 4g }\n"),
    (
        "every-field",
        FULL,
        """\
name: other
kind: autonomous
description: Another text.
model: { router: slow, budget_usd_per_day: 1 }
files:
  - { path: /srv/agents/vault, mode: rw }
tools:
  kagi: all
verbs:
  job_status: {}
delegates: []
max_inflight_delegations: 2
egress: [example.invalid]
shell: false
sandbox_tools: [read]
system_prompt: replace
sandbox: { cpus: 1, memory: 1g, max_resident_processes: 4, image: python }
skills: []
approval: [embed, job_status]
triggers:
  - enqueue: true
max_running_turns: 2
quiet: {}
""",
    ),
)


def _value(diff: Diff) -> dict[str, Json]:
    return {
        "changes": normalize(diff.changes),
        "changed": diff.changed,
        "refused": [one.field for one in diff.refused],
        "needs_switch": diff.needs_switch,
        "switch_mode": normalize(diff.switch_mode),
        "steps": normalize(diff.steps()),
        "reason": diff.reason(),
    }


def _vector(case_id: str, old_text: str, new_text: str) -> Vector:
    old, old_issues = parse_family(old_text)
    new, new_issues = parse_family(new_text)
    if old is None or new is None:
        raise ValueError(f"the classify case {case_id} does not parse: {old_issues} {new_issues}")

    given: dict[str, Json] = {"args": {"old": old_text, "new": new_text}}

    return accepted(case_id, given, _value(classify(old, new)))


def surfaces() -> tuple[Surface, ...]:
    return (
        Surface(
            name="family_file.classify",
            path="family_file.classify.json",
            entry=ENTRY,
            contract=CONTRACT,
            notes=NOTES,
            vectors=tuple(_vector(*pair) for pair in PAIRS),
        ),
    )
