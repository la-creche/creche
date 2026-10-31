"""Step 2's second input: what is running NOW (contract 06 §11).

**Root BUILDS this document. It is not a file anybody hands root.**

Contract 06 §11: "built by root at step 2 and consumed in the same step,
never written by operator-side code". So the document is built here, out of
four reads root can make for itself.

| Field | Where it comes from |
|---|---|
| `live` | `<install.to>/.release-version`, root's own stamp under a root-owned tree |
| `provided` | the manifest the last release stamped into each live tree |
| `latest` | the newest `<component>-v<version>` tag, from an injected reader |
| `facts` | the tag's commit, and a digest over that commit's source |

The last two are the only ones that leave the disk, and both arrive through
`Readers` so a test fakes them and the requester substitutes its own.

**Root's tag reader is the GitHub API and not the corpus clone.**
`facts.sha` has to come from GitHub
whatever happens — it is `provenance.tag_commit`, the same call P2 makes —
so a `latest` read out of the operator-owned corpus would let a planted
`noticeboard-v9.9.9` tag name a version GitHub has no Release for. Every `request
noticeboard` with no version would then refuse at step 2, for ever, on one tag a
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
from ..manifest import (
    MANIFEST_FILENAME,
    MANIFEST_FILENAME_YAML,
    ComponentManifest,
    Markup,
    parse_manifest,
)
from ..resolve import LATEST, TAG_FORMAT, deploying_versions
from ..silent import silent_manifest
from ..site import operator_home
from ..state import ReleaseState, SourceFacts
from ..tomlfile import FileKind

#: Written into the artifact by the switch, read back to learn what is live.
STAMP_FILE: Final = ".release-version"

#: The other thing the switch stamps into the artifact: the manifest root
#: resolved this version from. It is what makes a NON-deploying
#: component's `provides` and `requires` a fact rather than a claim: the
#: tree is root-owned and the file got there through a verified release.
#: Without it, step 2 read every manifest at a SHA the operator-written
#: live-state document supplied, and only the deploying one was ever tied
#: to a verified tag.
#:
#: The stamp has the name of the markup of its text. A tree that holds an
#: entry named `MANIFEST_STAMP` reads that stamp, and a tree that does not
#: reads `MANIFEST_STAMP_YAML`.
MANIFEST_STAMP: Final = f".{MANIFEST_FILENAME}"
MANIFEST_STAMP_YAML: Final = f".{MANIFEST_FILENAME_YAML}"

#: One YAML manifest is a page of YAML. The cap is here so a planted file
#: under an install tree cannot be read into memory. The read of a TOML stamp
#: stops one byte past the byte limit of `FileKind.MANIFEST`.
MAX_MANIFEST_BYTES: Final = 64 * 1024

#: A version is at most `999.999.999` plus a newline. The cap is here so a
#: planted file cannot be read into memory.
MAX_STAMP_BYTES: Final = 64

#: Where components install (contract 06 §1). Root-owned components go under
#: the first, and `attendance`, `caregiver` and `noticeboard` under the second, because
#: their units are the operator's user units and a user unit reads a tree the operator
#: owns. They are spelled HERE and not in `host.py`, which imports them,
#: because they are where `live` is read from and the requester — which may
#: not import `host.py` (`handover/AGENTS.md`) — reads the same stamps to
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


def stamp_name(markup: Markup) -> str:
    """The name of the manifest stamp of a text in `markup`."""
    return MANIFEST_STAMP if markup is Markup.TOML else MANIFEST_STAMP_YAML


@dataclass(frozen=True)
class StampedText:
    """The text of a manifest stamp, and the markup that its name gives."""

    text: str
    markup: Markup


def write_manifest_stamp(tree: Path, text: str, markup: Markup) -> None:
    """Stamp the manifest this version was resolved from into the staged
    tree, before it is switched in, under the name of its markup. Root
    writes it, root owns the tree."""
    (tree / stamp_name(markup)).write_text(text, encoding="utf-8")


def installed_manifest(tree: Path) -> StampedText | None:
    """The stamped manifest under a live tree, or None where no release
    has put one there yet.

    The tool reads `MANIFEST_STAMP` first and `MANIFEST_STAMP_YAML` second.
    A tree that holds an entry named `MANIFEST_STAMP` reads that entry
    alone: a link there, or a text that is not UTF-8, is no stamp.

    CONTRACT-QUESTION: contract 06 §10.2 puts the TOML stamp before the
    YAML stamp. It does not say what the tool reads when the TOML stamp is
    there and the tool cannot read it. The tool reads no stamp then, and
    the component takes row 4 of §10.2. The other reading takes the YAML
    stamp, which an older release wrote. A change costs the first `if` of
    this function and one test of `tests/test_handover_manifest_toml.py`."""
    toml_stamp = tree / MANIFEST_STAMP
    if toml_stamp.is_symlink() or toml_stamp.exists():
        return _stamp_of(_toml_stamp_text(toml_stamp), Markup.TOML)

    return _stamp_of(_yaml_stamp_text(tree / MANIFEST_STAMP_YAML), Markup.YAML)


def _stamp_of(text: str | None, markup: Markup) -> StampedText | None:
    return StampedText(text, markup) if text else None


def _toml_stamp_text(stamp: Path) -> str | None:
    """The text of a TOML stamp. The read stops one byte past the byte limit
    of `FileKind.MANIFEST`, so the parse refuses a larger file."""
    try:
        if stamp.is_symlink():
            return None

        with stamp.open("rb") as handle:
            data = handle.read(FileKind.MANIFEST.byte_limit + 1)

        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _yaml_stamp_text(stamp: Path) -> str | None:
    try:
        if stamp.is_symlink() or stamp.stat().st_size > MAX_MANIFEST_BYTES:
            return None

        return stamp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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

#: `()` to the commit at the `HEAD` of root's registry clone, or None when
#: the clone cannot say (contract 06 §9.2 rule 2).
RegistryHeadFn = Callable[[], str | None]


def _no_version(component: str) -> str | None:
    del component

    return None


def _no_sha(component: str, version: str) -> str | None:
    del component, version

    return None


def _no_digest(component: str, sha: str) -> str | None:
    del component, sha

    return None


def _no_head() -> str | None:
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
    registry_head: RegistryHeadFn = _no_head


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
    #: contract 06 §10's "it declares nothing" while it runs. `noticeboard` requires
    #: three contracts, so nothing would provide them and step 4 would
    #: refuse with C1.
    present: bool = False
    #: `<install.to>/.release-version`, or None for a first install.
    version: str | None = None
    #: The manifest the last release stamped in, parsed. None where no
    #: release has stamped one: a tree the cutover or the visit made.
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
    stamp = installed_manifest(tree)
    if stamp is None:
        return None

    try:
        found = parse_manifest(stamp.text, f"{name}/{stamp_name(stamp.markup)}", stamp.markup)
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
    """Contract 06 §9's `artifact_digest`, as this builder knows it: `null`.

    This builder runs before root reads a manifest, so it cannot know the
    kind of a component. Step 2 fills the value after that read, for each
    component that deploys and whose manifest says `binary`: the digest of
    root's copy of its archive (`steps.Release._copy_artifacts`). Step 8
    fills it again from the same copy. Each other kind keeps `null`. For
    `oci-image` that is so today because nothing in this repository pushes
    `playpen` to an OCI registry, so there is no registry digest to record.
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


#: The component whose servers come from the registry, and the registry's
#: own row (contract 06 §9).
MCP_SERVERS: Final = "mcp-servers"
REGISTRY_DATA: Final = "registry-data"


def _registry_facts(
    deploying: frozenset[str], readers: Readers, pinned: str | None
) -> tuple[dict[str, SourceFacts], tuple[str, ...]]:
    """The `registry-data` row of a set that deploys `mcp-servers`.

    Contract 06 §9: its `sha` is the registry commit the release reads
    `mcp/` at, and its digest is over `mcp` at that commit. So the manifest
    hash, and the tap, bind the server files. Step 2 reads the commit at
    the clone's `HEAD`. Step 8 passes the approved commit as `pinned`, so a
    registry merge after the tap is not drift (§9.2 rule 6). Every other
    set gets no row, as before.
    """
    if MCP_SERVERS not in deploying:
        return {}, ()

    sha = pinned if pinned is not None else readers.registry_head()
    if sha is None:
        return {}, ("no registry commit: the mcp/ files are not bound",)

    digest = readers.input_digest(REGISTRY_DATA, sha)
    if digest is None:
        return {}, (f"no registry commit {sha[:12]} in the corpus: the mcp/ files are not bound",)

    return {REGISTRY_DATA: SourceFacts(sha=sha, input_digest=digest, artifact_digest=None)}, ()


def build_state(
    roots: tuple[Path, ...],
    wanted: Mapping[str, str],
    readers: Readers,
    registry_sha: str | None = None,
) -> BuiltState:
    """Contract 06 §11's document, built from root's own reads.

    The order is forced by what each part needs. `live` and `provided` come
    off the disk. `latest` turns the request's `latest` words into numbers.
    Only then is it known what would DEPLOY, and only then is it known
    which components need a tag resolved into source facts.

    `registry_sha` is step 8's approved registry commit, and None at step 2.
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
    registry, why = _registry_facts(deploying, readers, registry_sha)
    facts.update(registry)

    return BuiltState(
        state=ReleaseState(live=live, provided=provided, latest=latest, facts=facts),
        trees=trees,
        notes=notes + more + why,
    )
