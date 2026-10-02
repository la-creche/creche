"""Step 2's second input: what is running NOW (contract 06 §11).

**Root BUILDS this document. It is not a file anybody hands root.**

Contract 06 §11: "built by root at step 2 and consumed in the same step,
never written by operator-side code". So the document is built here, out of
four reads root can make for itself.

| Field | Where it comes from |
|---|---|
| `live` | `<install.to>/.release-version`, root's own stamp under a root-owned tree |
| `provided` | the `component.yaml` the last release stamped into each live tree |
| `latest` | the newest `<component>-v<version>` tag, from an injected reader |
| `facts` | the tag's commit, and a digest over that commit's source |

The last two are the only ones that leave the disk, and both arrive through
`Readers` so a test fakes them and the requester substitutes its own.

**Root's tag reader is the GitHub API and not the corpus clone.**
`facts.sha` has to come from GitHub
whatever happens — it is `provenance.tag_commit`, the same call P2 makes —
so a `latest` read out of the operator-owned corpus would let a planted
`ui-v9.9.9` tag name a version GitHub has no Release for. Every `request
ui` with no version would then refuse at step 2, for ever, on one tag a
operator-side process wrote. Reading both from one authority closes that. The
REQUESTER reads the corpus instead, because it is advisory, it runs as
the operator, and it may hold no token: it says what it could not check rather
than refusing, because root re-derives all of it.

**A `latest` map is built only for the components the request names.** Root
resolves `latest` for nothing else (`resolve._target_version`), and a tag
read for a component this release does not touch is a call that can fail
and stop a release it has nothing to do with.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ..catalog import CATALOG_BY_NAME, CONTRACT_OWNER, ContractId, Releases
from ..errors import Refusal
from ..manifest import ComponentManifest, parse_manifest
from ..resolve import LATEST, TAG_FORMAT, deploying_versions
from ..silent import silent_manifest
from ..site import operator_home
from ..state import ReleaseState, SourceFacts

#: Written into the artifact by the switch, read back to learn what is live.
STAMP_FILE: Final = ".release-version"

#: The other thing the switch stamps into the artifact: the `component.yaml`
#: root resolved this version from. It is what makes a NON-deploying
#: component's `provides` and `requires` a fact rather than a claim: the
#: tree is root-owned and the file got there through a verified release.
#: Without it, step 2 read every manifest at a SHA the operator-written
#: live-state document supplied, and only the deploying one was ever tied
#: to a verified tag.
MANIFEST_STAMP: Final = ".component.yaml"

#: One manifest is a page of YAML. The cap is here so a planted file under
#: an install tree cannot be read into memory.
MAX_MANIFEST_BYTES: Final = 64 * 1024

#: A version is at most `999.999.999` plus a newline. The cap is here so a
#: planted file cannot be read into memory.
MAX_STAMP_BYTES: Final = 64

#: Where components install (contract 06 §1). Root-owned components go under
#: the first, and `sessiond`, `managerd` and `ui` under the second, because
#: their units are the operator's user units and a user unit reads a tree the operator
#: owns. They are spelled HERE and not in `host.py`, which imports them,
#: because they are where `live` is read from and the requester — which may
#: not import `host.py` (`release/AGENTS.md`) — reads the same stamps to
#: preview what root will resolve.
#:
#: The operator's root is under the operator's home, which the site names
#: (`site.py`), so it is a function and not a constant: it is read when a
#: caller asks, never when this module is imported.
INSTALL_ROOT: Final = "/opt/components"
OPERATOR_COMPONENTS: Final = ".local/components"


def operator_install_root() -> str:
    """Where the operator's components install, e.g.
    `/home/operator/.local/components`."""
    return f"{operator_home()}/{OPERATOR_COMPONENTS}"


def default_install_roots() -> tuple[Path, ...]:
    """Both install roots of this host: root's, then the operator's."""
    return (Path(INSTALL_ROOT), Path(operator_install_root()))


def write_stamp(tree: Path, version: str) -> None:
    """Stamp a staged tree, before it is switched in."""
    (tree / STAMP_FILE).write_text(f"{version}\n", encoding="utf-8")


def write_manifest_stamp(tree: Path, text: str) -> None:
    """Stamp the manifest this version was resolved from into the staged
    tree, before it is switched in. Root writes it, root owns the tree."""
    (tree / MANIFEST_STAMP).write_text(text, encoding="utf-8")


def installed_manifest(tree: Path) -> str | None:
    """The stamped manifest under a live tree, or None where no release
    has put one there yet."""
    stamp = tree / MANIFEST_STAMP
    try:
        if stamp.is_symlink() or stamp.stat().st_size > MAX_MANIFEST_BYTES:
            return None

        text = stamp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    return text or None


def installed_version(tree: Path) -> str | None:
    """What the stamp under a live tree says, or None for a first install."""
    stamp = tree / STAMP_FILE
    try:
        if stamp.is_symlink() or stamp.stat().st_size > MAX_STAMP_BYTES:
            return None

        text = stamp.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None

    return text or None


# -- the readers the builder cannot make for itself -----------------------

#: `(component)` to the newest released version, or None when that
#: component has no tag yet. Root's raises a `Refusal` for a reader it
#: cannot reach at all, because a predicate that cannot ask has not passed.
#: The requester's answers None and the builder records a note.
NewestVersionFn = Callable[[str], str | None]

#: `(component, version)` to the 40-hex commit the tag names, or None when
#: the reader could not say. Same split: root refuses, the requester notes.
TagShaFn = Callable[[str, str], str | None]

#: `(component, sha)` to contract 06 §9's `input_digest`, or None when the
#: clone holding the source does not have that commit yet.
InputDigestFn = Callable[[str, str], str | None]


def _no_version(component: str) -> str | None:
    del component

    return None


def _no_sha(component: str, version: str) -> str | None:
    del component, version

    return None


def _no_digest(component: str, sha: str) -> str | None:
    del component, sha

    return None


@dataclass(frozen=True)
class Readers:
    """Everything the document needs and the disk cannot answer.

    The default is the empty world: no tag, no commit, no digest. It is
    what a pure resolver test wants, and it is also the fail-closed end —
    a builder nobody wired resolves `latest` to nothing and refuses.
    """

    newest_version: NewestVersionFn = _no_version
    tag_sha: TagShaFn = _no_sha
    input_digest: InputDigestFn = _no_digest


@dataclass(frozen=True)
class LiveTree:
    """What root found under the install roots for one component.

    One read of the disk, one answer. `steps.py` decides which manifest it
    may believe from this same answer, because two readers of one
    directory are two answers to one question.
    """

    #: Whether the install directory is there at all. It is a FIELD and not
    #: `version is not None`: a tree the cutover or the visit made carries
    #: no stamp, and read as "this component is not installed" it would get
    #: contract 06 §10's "it declares nothing" while it runs. `ui` requires
    #: three contracts, so nothing would provide them and step 4 would
    #: refuse with C1.
    present: bool = False
    #: `<install.to>/.release-version`, or None for a first install.
    version: str | None = None
    #: The `component.yaml` the last release stamped in, parsed. None where
    #: no release has stamped one: a tree the cutover or the visit made.
    manifest: ComponentManifest | None = None


@dataclass(frozen=True)
class BuiltState:
    """Contract 06 §11's document, plus what root learned building it."""

    state: ReleaseState
    #: Per component, what the install roots hold. `steps.py` reads it
    #: rather than walking the roots a second time.
    trees: dict[str, LiveTree] = field(default_factory=dict[str, LiveTree])
    #: One line per thing the builder could not establish. Root puts them
    #: in the ledger, the requester prints them.
    notes: tuple[str, ...] = ()


def read_trees(roots: tuple[Path, ...]) -> dict[str, LiveTree]:
    """Walk the install roots once, for every component in the catalog."""
    found: dict[str, LiveTree] = {}
    for name in sorted(CATALOG_BY_NAME):
        found[name] = _one_tree(roots, name)

    return found


def _one_tree(roots: tuple[Path, ...], name: str) -> LiveTree:
    """The first root that holds a directory for this component.

    A DIRECTORY, not a stamp. A component installs under exactly one root
    (contract 06 §1), so the first one that has it is the one that has it,
    and whether it carries a stamp is a separate question with a separate
    answer.
    """
    for root in roots:
        tree = root / name
        if not tree.is_dir() or tree.is_symlink():
            continue

        return LiveTree(
            present=True,
            version=installed_version(tree),
            manifest=_stamped_manifest(tree, name),
        )

    return LiveTree()


def _stamped_manifest(tree: Path, name: str) -> ComponentManifest | None:
    """The manifest under a live tree, parsed by the real parser.

    A stamped file that does not parse, or that names another component, is
    treated as absent: the component then falls to contract 06 §10.2's
    fourth row, which names it on the phone as unverified. Refusing here
    would let one unreadable byte under one tree stop every release.
    """
    text = installed_manifest(tree)
    if text is None:
        return None

    try:
        found = parse_manifest(text, f"{name}/{MANIFEST_STAMP}")
    except Refusal:
        return None

    if found.name != name:
        return None

    return found


def _live_of(trees: Mapping[str, LiveTree]) -> dict[str, str | None]:
    """Contract 06 §11's `live`: every releasable component's stamp."""
    return {
        name: trees[name].version
        for name in sorted(trees)
        if CATALOG_BY_NAME[name].releases is not Releases.NO
    }


def _provided_of(trees: Mapping[str, LiveTree]) -> dict[ContractId, tuple[int, int]]:
    """Contract 06 §11's `provided`, out of the stamped manifests.

    Only the component contract 06 §3 names as a contract's owner is read
    for it, which is rule C3 applied to what is LIVE. A tree carries a
    manifest root wrote, so this is belt and braces — and it costs one
    lookup to make a future stamped file unable to claim another's
    contract.

    A contract absent here is a first install, and §11 says C4 does not
    apply to it. A host where no tree carries a stamp, as the host was at
    its first release, resolves against an empty map, and
    `test_the_first_release_resolves_against_an_empty_provided` is it.
    """
    provided: dict[ContractId, tuple[int, int]] = {}
    for name in sorted(trees):
        manifest = trees[name].manifest
        if manifest is None:
            continue

        for item in manifest.provides:
            if CONTRACT_OWNER.get(item.contract) != name:
                continue

            provided[item.contract] = (item.major, item.minor)

    return provided


def _latest_of(
    wanted: Mapping[str, str], readers: Readers
) -> tuple[dict[str, str], tuple[str, ...]]:
    """What `latest` means, for the components this request names by it."""
    latest: dict[str, str] = {}
    notes: list[str] = []
    for name in sorted(wanted):
        if wanted[name] != LATEST or name not in CATALOG_BY_NAME:
            continue

        found = readers.newest_version(name)
        if found is None:
            notes.append(f"no released tag for {name}: 'latest' names nothing")
            continue

        latest[name] = found

    return latest, tuple(notes)


def unstamped_names(trees: Mapping[str, LiveTree], deploying: frozenset[str]) -> tuple[str, ...]:
    """Contract 06 §10.2's fourth row: installed, not deploying, not stamped.

    Root has to read these manifests at a SHA rather than out of a tree it
    owns, so they are the components §2.5's `review` field calls suspect.
    It is `present` and not `version is not None`, because a tree the
    cutover or the visit made is installed and unstamped.
    """
    return tuple(
        name
        for name in sorted(trees)
        if name not in deploying and trees[name].present and trees[name].manifest is None
    )


#: `(component)` to that component's manifest AT A COMMIT, or None when the
#: caller has no source for it. Root's fetches a clone at a tag step 3
#: verifies; the requester's reads the working tree `--root` named. One
#: signature, because the DECISION about which source a component's manifest
#: comes from must have one implementation — root and the requester
#: disagreeing about the set is how a preview promises what root refuses.
AtCommitFn = Callable[[str], ComponentManifest | None]


@dataclass(frozen=True)
class Chosen:
    """Contract 06 §10.2's table, applied to one release."""

    manifests: dict[str, ComponentManifest] = field(default_factory=dict[str, ComponentManifest])
    #: Read at a commit rather than out of a tree root owns (rows 4 and 5).
    #: §2.5's `review` field counts these as `suspect`.
    unverified: tuple[str, ...] = ()
    #: Installed, unstamped, and no source for it at all. Root can say
    #: nothing about these, and says so rather than letting a silent
    #: manifest read as a fact.
    unreadable: tuple[str, ...] = ()


def select_manifests(
    deploying: frozenset[str], trees: Mapping[str, LiveTree], at_commit: AtCommitFn
) -> Chosen:
    """Which manifest root believes for each component, and why.

    Contract 06 §10.2's five rows, in the order they are tried. The
    deploying set goes FIRST, because root's `at_commit` reads a row-5
    manifest out of a clone a deploying component left behind.

    | # | When | Source |
    |---|---|---|
    | 1 | it deploys | a clone at the resolved SHA, which step 3 ties to a bot-cut Release |
    | 2 | its tree carries a stamped manifest | that file: root wrote it, into a tree root owns |
    | 3 | it has no install tree at all | nothing. It declares nothing |
    | 4 | it is stamped with a version and has no stamped manifest | a clone at its own live tag |
    | 5 | it is installed and carries no stamp at all | a clone this release already fetched |
    """
    found: dict[str, ComponentManifest] = {}
    unverified: list[str] = []
    unreadable: list[str] = []
    for name in sorted(deploying):
        picked = at_commit(name)
        if picked is not None:
            found[name] = picked

    for name in sorted(CATALOG_BY_NAME):
        if name in found:
            continue

        tree = trees.get(name, LiveTree())
        if tree.manifest is not None:
            found[name] = tree.manifest
            continue

        if not tree.present:
            found[name] = silent_manifest(name)
            continue

        picked = at_commit(name)
        if picked is None:
            unreadable.append(name)
            found[name] = silent_manifest(name)
            continue

        unverified.append(name)
        found[name] = picked

    return Chosen(manifests=found, unverified=tuple(unverified), unreadable=tuple(unreadable))


def _artifact_digest(component: str) -> str | None:
    """Contract 06 §9's `artifact_digest`.

    `null` for every kind but `oci-image`, and `null` for that one too
    today: nothing in this repository pushes `sandbox-image` to an OCI
    registry, so there is no registry digest to record. Saying so here,
    once, is better than nine `None`s a reader has to infer a reason for.
    Contract 06 §9 carries the same sentence.
    """
    del component

    return None


def _facts_of(
    targets: Mapping[str, str], readers: Readers
) -> tuple[dict[str, SourceFacts], tuple[str, ...]]:
    """§9's source columns, for the components root will act on and no other.

    `targets` maps a component to the version whose tag root must resolve:
    the version a deploying component moves TO, and the version a
    §10.2-row-4 component is already at. Everything else gets no row, and
    §9's columns read `null` for it — root fetched no source for it, ran no
    predicate over it, and a column root cannot vouch for is not a record.
    """
    facts: dict[str, SourceFacts] = {}
    notes: list[str] = []
    for name in sorted(targets):
        version = targets[name]
        sha = readers.tag_sha(name, version)
        if sha is None:
            tag = TAG_FORMAT.format(name=name, version=version)
            notes.append(f"no commit for {tag}: its source facts are unknown here")
            continue

        digest = readers.input_digest(name, sha)
        if digest is None:
            notes.append(f"no source for {name} at its resolved commit: no input digest")

        facts[name] = SourceFacts(
            sha=sha, input_digest=digest, artifact_digest=_artifact_digest(name)
        )

    return facts, tuple(notes)


def build_state(roots: tuple[Path, ...], wanted: Mapping[str, str], readers: Readers) -> BuiltState:
    """Contract 06 §11's document, built from root's own reads.

    The order is forced by what each part needs. `live` and `provided` come
    off the disk. `latest` turns the request's `latest` words into numbers.
    Only then is it known what would DEPLOY, and only then is it known
    which components need a tag resolved into source facts.
    """
    trees = read_trees(roots)
    live = _live_of(trees)
    provided = _provided_of(trees)
    latest, notes = _latest_of(wanted, readers)
    partial = ReleaseState(live=live, provided=provided, latest=latest)

    targets = dict(deploying_versions(partial, dict(wanted)))
    deploying = frozenset(targets)
    for name in unstamped_names(trees, deploying):
        version = trees[name].version
        if version is not None:
            targets[name] = version

    facts, more = _facts_of(targets, readers)

    return BuiltState(
        state=ReleaseState(live=live, provided=provided, latest=latest, facts=facts),
        trees=trees,
        notes=notes + more,
    )
