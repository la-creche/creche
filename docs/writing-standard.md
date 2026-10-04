# Writing standard: ASD-STE100

Every Markdown document in this repository is written in ASD-STE100
Simplified Technical English (STE). This is a rule of the repository.
`AGENTS.md` makes it binding for every contributor and for every agent.

## 1. Scope

The rule applies to every `.md` file in this tree: `README.md`,
`AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md` and every file under `docs/`.

The rule does not apply to:

- Code comments and docstrings. Apply the rules to a new comment when the
  result stays correct.
- Test fixtures. A `.md` file under a `tests/` or `fixtures/` directory is
  test data, not a document.
- Quoted text. An error message, a command or a log line is quoted as it is.

## 2. What the standard is

ASD-STE100 is a controlled language. The AeroSpace and Defence Industries
Association of Europe (ASD) maintains it. The current issue is Issue 9. The
standard has two parts:

1. Writing rules. They limit sentence length, grammar and structure.
2. A dictionary of approved words. Each approved word has one meaning.

This repository applies the writing rules in full. It does not reproduce the
dictionary, because the dictionary is not licensed for redistribution. This
repository does not claim certified compliance with the standard. Get the
official text from the ASD download page:
<https://www.asd-ste100.org/STE_downloads.html>.

## 3. The writing rules

| Rule | Do | Do not |
|---|---|---|
| Active voice | "The reconciler writes the grant file." | "The grant file is written." |
| One instruction per sentence | "Open the file. Read line 3." | "Open the file and read line 3, then check it." |
| Sentence length | 20 words or less in an instruction. 25 words or less in a description. | Compound sentences with stacked clauses. |
| Simple tenses | Present, past, future, imperative. | Present perfect and progressive forms, unless the simple form loses meaning. |
| Articles | Keep "a", "an" and "the". | "Files not backed up are lost." |
| Noun clusters | Three words or less: "grant file path". | "per-family status document poll interval". |
| Paragraphs | One topic. Six sentences or less. | A paragraph that covers two topics. |
| Sequences | A numbered list for three or more steps. | A sequence inside one sentence. |
| Punctuation | A period ends a sentence. | A semicolon. An em dash. |
| Modality | Keep "may" and "can" where the fact is uncertain. | Promote a hedge to a fact. |
| Warnings | State the condition first, then the instruction. | A warning buried in a paragraph. |

## 4. Word rules

1. One word has one meaning. One meaning has one word. Do not rotate
   synonyms.
2. Use the plainest common word. Write "use", not "utilize". Write "start",
   not "spin up".
3. Use a verb, not a noun that hides the verb. Write "validate the file",
   not "perform validation of the file".
4. Do not use idioms, slang or figurative language.
5. Do not use marketing adjectives. Replace the adjective with the measurement
   that supports the claim, or delete it.
6. Define a technical name once, at first use. Then use the same name every
   time.

## 5. The project dictionary

These are the technical names of this repository. Use each name as it is
written here. Do not use a synonym for one.

| Name | Meaning |
|---|---|
| component | One unit of release: a directory with a `component.yaml`. |
| family | One agent definition: one `family.yaml` in the registry. |
| registry | The git checkout that holds every family file and every MCP server file. |
| sandbox | One microVM that runs one family. |
| session | One conversation in one family. |
| turn | One prompt and its answer, inside a session. |
| door | A process that turns outside input into a turn. |
| grant | What a family may do. The chaperone reads it from a grant file. |
| verb | An action the chaperone executes itself, not through an MCP server. |
| the chaperone | The Policy Enforcement Point (PEP). Every action outside a sandbox passes through it. |
| the operator | The person who runs a deployment. |
| the host | The machine that runs a deployment. |
| the site file | `/etc/creche/site.env`. It holds what differs between deployments. |
| deploy | Put this repository at `/opt/creche` and restart one unit. |
| release | Replace one component tree and restart its unit. |
| converge | Make the host match the family file. The caregiver does this. |

Approved verbs for actions in this repository: read, write, start, stop,
restart, refuse, allow, deny, validate, converge, release, deploy, install,
remove, mint, rotate, publish, mount, bind, fetch, build, swap, restore.

## 6. Sanitization

A document in this repository names no deployment. Do not write:

- A host name, a LAN address or a domain of a deployment.
- A person, a Linux account name or a home directory of a deployment.
- A GitHub owner. Write `<owner>` instead.
- A phone target, a backup host or a time zone of a deployment.

Write "the operator", "the host", "the host's LAN address" and "the backup
host". In an example that needs an address, use `192.0.2.10` and the other
TEST-NET-1 addresses.

The design documents and the contracts of the original deployment are in a
private repository. A citation such as `docs/rework/spec.md` or
`contract 04 §6` resolves there. Keep the citation as it is.

## 7. Checklist before you commit a document

1. Read the first line. It states what the document is for.
2. Read every sentence once. Delete the first sentence if it announces what
   follows. Delete the last sentence if it repeats what came before.
3. Find every semicolon and every em dash. Split the sentence.
4. Find every sentence over 20 words in an instruction. Split it.
5. Find every "and then". Make a numbered list.
6. Find every word with two meanings in context. Replace it.
7. Check section 6. The document names no deployment.

## 8. Enforcement

No tool checks this rule. The reviewer checks it. A pull request that adds
prose which breaks a rule in this document is not merged until the prose is
fixed. A `.md` change alone runs only the tests marked `docs`
(`bin/lib/docsrule.sh`), so a prose fix is cheap to land.
