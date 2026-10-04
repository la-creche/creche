"""`handover`: the rework release command the operator types.

**The name.** `handover` is this package's, it names the component
(`handover/component.yaml`, `name: handover`), and nothing else declares
it.

Five subcommands, and only the last two write anything.

1. `check` validates what is on disk: every manifest parses, every name
   matches contract 06 §1, `depends_on` is acyclic, and rules C1 to C3 hold
   over the whole set.
2. `resolve <name>=<version> …` says what a release would do. Given a live
   state that carries source facts and a release id, it also prints contract
   06 §9's resolved manifest and its hash.
3. `allocate-tags` says what tag a merge allocates (contract 06 §2.1).
   `--cut <file>` plans nothing: it prints each changed path of the file,
   cut to the prefix the planner reads, for `bin/allocate-tags.sh`.
4. `request <name>[@<version>] …` files ONE release request into the spool
   (`stage7-releases.md` §2.3). It prints §2.5's seven fields first, so the operator
   reads what the phone will show BEFORE the file exists, and it refuses what
   the executor would refuse with the executor's own reason. `--dry-run`
   prints and writes nothing. `--wait [SECONDS]` fetches and resolves again
   every ten seconds until every named component would move, so a request
   typed right after a merge waits for its tag instead of being
   refused. A dry run that waits fetches too, and still files nothing.
5. `follow <name> …` is `request` on a timer (`follow/`). It fetches, reads
   which of the named components a newer tag would move, and files ONE
   request for that set. It files a set once, after it read the same set on
   two runs, and it approves nothing. A run with nothing new prints one
   line and exits 0.

Exit codes: 0 pass, 1 refusal, 2 a usage mistake or a spool this host cannot
write.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

from .allocate import cut_paths, plan_tags, read_lines
from .catalog import Repo, releasable_names
from .contracts import ContractRow
from .corpus import (
    BEHIND_NOTE,
    CORPUS_ROOT,
    FETCH_NOTE,
    CorpusReport,
    local_readers,
    refresh,
    repos_of,
)
from .discovery import ManifestSet, discover, require_complete
from .errors import Refusal, RefusalCode, safe_token
from .follow import (
    REQUESTED_BY as FOLLOWED_BY,
)
from .follow import (
    Answer,
    Step,
    asked_marker,
    decide,
    default_marker,
    done_dir_of,
    moving_of,
    read_answer,
    read_marker,
    seen_marker,
    write_marker,
)
from .requester import (
    REQUESTS_PATH,
    BuiltState,
    Chosen,
    RequesterError,
    build_state,
    default_install_roots,
    file_request,
    plan_request,
    preview_of,
    select_manifests,
)
from .requester.preview import HEADER
from .resolve import Resolution, build_document, deploying_names, parse_request, resolve
from .site import require_complete as require_complete_site
from .state import MAX_STATE_BYTES, ReleaseState, parse_state

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

PROG = "handover"

#: `request` takes `<component>[@<version>]`, which is the tag's own shape
#: (`<name>-v<version>`, contract 06 §2.1). `resolve` keeps `NAME=VERSION`:
#: it is the resolver's existing argument form and changing it would break
#: every caller for a cosmetic gain.
REQUEST_ITEM_SEPARATOR = "@"

#: §2.3: what a request writes instead of a number.
LATEST = "latest"

#: §2.3's `requested_by` for a request the operator types by hand.
HUMAN = "human"

#: The clock the document records when the caller gives no better one. A
#: resolve with no `--id` builds no document at all, so this value only
#: reaches a run the caller asked to be reproducible.
DEFAULT_RESOLVED_AT = 0.0

DEFAULT_REQUESTED_BY = "human"

#: A tag list grows with the repository. One mebibyte is far above what
#: `git tag --list` prints for any of the three repos.
MAX_INPUT_BYTES = 1024 * 1024

#: `--wait` with no number, in seconds. A tag reaches GitHub once the merged
#: tree passed its suite, about four minutes after the merge, so ten minutes
#: is that with room for a slow runner.
DEFAULT_WAIT_S = 600

#: Seconds between two polls under `--wait`. One poll is one `git fetch` per
#: repository the request touches.
POLL_S = 10

#: The refusal when no named component moves. `--wait` adds `WAITED`.
NOTHING_DEPLOYS = "nothing would deploy: every named component is already at that version"

#: `--wait`'s refusal when one named component moves and another does not.
NO_TAG_MOVES = "no tag moves {names}"

#: What `--wait` prints between two polls, and adds to its deadline refusal.
WAITING = "waiting for a tag that moves {names} ({elapsed}s of {limit}s)"
WAITED = "after waiting {limit}s"

#: `follow`'s clock. The marker carries a time from one run to the next, so
#: `monotonic` cannot serve. A module name, so a test swaps it like `sleep`.
wall_time = time.time

Document = dict[str, object]
Handler = Callable[[argparse.Namespace], int]


def _roots(values: list[str] | None) -> list[Path]:
    """Repository roots to search. No value means the working directory."""
    if not values:
        return [Path.cwd()]

    return [Path(value) for value in values]


def _read_state(path: str | None) -> ReleaseState:
    """Read the live-state document, or resolve against an empty world."""
    if path is None:
        return ReleaseState()

    file = Path(path)
    # Cap before the read, not after: the file is input like any other.
    if file.stat().st_size > MAX_STATE_BYTES:
        detail = f"larger than {MAX_STATE_BYTES} bytes"
        raise Refusal(RefusalCode.STATE, file.name, detail)

    return parse_state(file.read_text(encoding="utf-8"), file.name)


def _print_components(resolution: Resolution) -> None:
    print(f"{'component':<16}{'action':<11}{'from':<10}{'to':<10}tag")
    for item in resolution.components:
        line = (
            f"{item.name:<16}{item.action!s:<11}"
            f"{item.from_version or '-':<10}{item.to_version or '-':<10}{item.tag() or '-'}"
        )
        print(line)


def _consumer_text(row: ContractRow) -> str:
    if not row.consumers:
        return "none"

    return ", ".join(f"{item.name}@{item.major}.{item.min_minor}" for item in row.consumers)


def _print_contracts(rows: tuple[ContractRow, ...]) -> None:
    print("\ncontracts")
    for row in rows:
        print(f"  {row.contract!s:<20}{row.provider:<16}{row.version():<8}{_consumer_text(row)}")


def _print_missing(manifests: ManifestSet) -> None:
    if not manifests.missing:
        return

    print(f"\nmissing: {', '.join(manifests.missing)}")


def _plan_report(resolution: Resolution, manifests: ManifestSet) -> Document:
    return {
        "ok": True,
        "components": [
            {
                "name": item.name,
                "action": str(item.action),
                "from_version": item.from_version,
                "to_version": item.to_version,
                "tag": item.tag(),
            }
            for item in resolution.components
        ],
        "order": list(resolution.order),
        "contracts": [
            {
                "contract": str(row.contract),
                "provider": row.provider,
                "version": row.version(),
                "consumers": [item.name for item in row.consumers],
            }
            for row in resolution.contracts
        ],
        "missing": list(manifests.missing),
    }


#: Said once when a root is still incomplete right after a fetch: the
#: hourly `code-corpus-sync.timer` is the more likely cause than a missing
#: merge, and this names the one command the operator runs instead of guessing.
CORPUS_SYNC_HINT = (
    "the merge may not be in the corpus yet: run systemctl --user start code-corpus-sync.service"
)


def _found(args: argparse.Namespace, fetched: CorpusReport | None = None) -> ManifestSet:
    """Every manifest under the roots, complete unless `--partial` says not.

    `fetched` is a report from THIS call's own corpus fetch, and only
    `_run_request` ever has one: `check` and `resolve` touch no corpus.
    A root still incomplete right after that fetch gets one more line on
    its refusal, because the fetch already ran and the likelier reason is
    the corpus's own hourly pull, not a merge that never happened.
    """
    manifests = discover(_roots(args.root))
    if args.partial:
        return manifests

    try:
        require_complete(manifests)
    except Refusal as refusal:
        raise _with_sync_hint(refusal, fetched) from None

    return manifests


def _with_sync_hint(refusal: Refusal, fetched: CorpusReport | None) -> Refusal:
    if fetched is None:
        return refusal

    return Refusal(refusal.code, refusal.subject, f"{refusal.detail}; {CORPUS_SYNC_HINT}")


def _run_check(args: argparse.Namespace) -> int:
    """Validate what is on disk. Contract 06 §10, without a release."""
    if args.site:
        require_complete_site()

    manifests = _found(args)
    resolution = resolve(manifests.manifests(), ReleaseState(), {})
    if args.json:
        print(json.dumps(_plan_report(resolution, manifests), indent=2))

        return EXIT_OK

    print(f"components: {len(manifests.found)}")
    _print_contracts(resolution.contracts)
    _print_missing(manifests)
    print("\ncheck: PASS")

    return EXIT_OK


def _document_of(
    args: argparse.Namespace, resolution: Resolution, state: ReleaseState
) -> Document | None:
    """Contract 06 §9's document, when the caller supplied what it needs."""
    if args.id is None:
        return None

    return build_document(resolution, state, args.id, args.requested_by, args.resolved_at)


def _run_resolve(args: argparse.Namespace) -> int:
    manifests = _found(args)
    state = _read_state(args.state)
    resolution = resolve(manifests.manifests(), state, parse_request(args.components))
    document = _document_of(args, resolution, state)

    if args.json:
        report = _plan_report(resolution, manifests)
        report["manifest"] = document
        print(json.dumps(report, indent=2))

        return EXIT_OK

    _print_components(resolution)
    print(f"\norder: {', '.join(resolution.order) or 'nothing to deploy'}")
    _print_contracts(resolution.contracts)
    _print_missing(manifests)
    if document is not None:
        print(f"\nmanifest_sha256: {document['manifest_sha256']}")

    return EXIT_OK


def _lines_of(path: str | None, label: str) -> tuple[str, ...]:
    """One newline-separated input file. Absent means nothing was gathered."""
    if path is None:
        return ()

    file = Path(path)
    if file.stat().st_size > MAX_INPUT_BYTES:
        detail = f"{label} is larger than {MAX_INPUT_BYTES} bytes"
        raise Refusal(RefusalCode.REQUEST, label, detail)

    return read_lines(file.read_text(encoding="utf-8"), label)


def _run_cut(args: argparse.Namespace) -> int:
    """Each changed path of one range, cut to the prefix that decides its
    components (`allocate.cut_path`). It plans nothing.

    The file is `git diff --name-only` over a range, which can hold more
    lines than the planner takes. So it is read one line at a time, with no
    cap: the cut is what puts a range under the cap. A path that is not
    UTF-8 still has a first segment, so it is decoded and never refused.
    """
    with Path(args.cut).open(encoding="utf-8", errors="replace") as changed:
        for line in cut_paths((raw.rstrip("\n") for raw in changed), Repo(args.repo)):
            print(line)

    return EXIT_OK


def _run_allocate(args: argparse.Namespace) -> int:
    """What tag this merge allocates. It creates nothing itself."""
    if args.cut is not None:
        return _run_cut(args)

    plans = plan_tags(
        paths=_lines_of(args.paths, "changed paths"),
        levels=_lines_of(args.levels, "bump levels"),
        tags=_lines_of(args.tags, "tags"),
        at_sha=_lines_of(args.at_sha, "tags at this commit"),
        repo=Repo(args.repo),
    )

    if args.json:
        rows = [
            {
                "component": item.component,
                "outcome": str(item.outcome),
                "tag": item.tag,
                "from_version": item.from_version,
                "detail": item.detail,
            }
            for item in plans
        ]
        print(json.dumps({"ok": True, "tags": rows}, indent=2))

        return EXIT_OK

    for item in plans:
        print(f"{item.outcome:<9}{item.tag:<28}{item.from_version or '-':<10}{item.detail}")

    if not plans:
        print("no component touched: no tag, no Release")

    return EXIT_OK


def _wanted(items: list[str]) -> dict[str, str]:
    """`<component>[@<version>]` to §2.3's map. No version means `latest`.

    The shapes themselves are not checked here: `plan_request` hands the
    whole body to the executor's own parser, so one refusal comes from one
    place and reads the same on both sides.
    """
    wanted: dict[str, str] = {}
    for item in items:
        name, found, version = item.partition(REQUEST_ITEM_SEPARATOR)
        wanted[name] = version if found else LATEST

    return wanted


def _local_hash(
    args: argparse.Namespace, resolution: Resolution, state: ReleaseState, request_id: str
) -> str | None:
    """Contract 06 §9's hash over THIS tree, or None.

    It is never the hash the tap binds to (`requester/preview.py`), so a
    live-state document that carries no source facts costs a printed line
    and not a refusal.
    """
    try:
        document = build_document(resolution, state, request_id, args.requested_by, 0.0)
    except Refusal as refusal:
        if refusal.code is not RefusalCode.STATE:
            raise

        # stderr, so `--json` still prints one parseable object.
        print(f"note: {refusal.detail}; no local hash", file=sys.stderr)

        return None

    return str(document["manifest_sha256"])


def _request_state(args: argparse.Namespace, wanted: dict[str, str]) -> BuiltState:
    """The live state, built by the SAME code root builds its own with.

    Root builds contract 06 §11's document at step 2 out of the install
    trees and the tags, and this side runs that builder as the operator against
    the corpus.
    Every line it could not establish is printed (`_print_notes`), and none
    of them refuses: root re-derives all of it, so a preview that says less
    than the phone is a reader that could not look, never a different answer.
    """
    if args.state is not None:
        return BuiltState(state=_read_state(args.state))

    readers = local_readers(Path(args.corpus))

    return build_state(tuple(_install_roots(args)), wanted, readers)


def _print_notes(built: BuiltState) -> None:
    """The builder's notes, once, for the poll the command acts on. A wait
    would otherwise repeat them every `POLL_S` seconds."""
    for line in built.notes:
        print(f"note: {line}", file=sys.stderr)


def _install_roots(args: argparse.Namespace) -> list[Path]:
    """Where this host installs components. `--install-root` is repeatable
    and its default is the executor's own two (`executor/host.py`), so the
    preview reads the same stamps root will."""
    if not args.install_root:
        return list(default_install_roots())

    return [Path(value) for value in args.install_root]


def _chosen_manifests(
    args: argparse.Namespace,
    manifests: ManifestSet,
    built: BuiltState,
    state: ReleaseState,
    wanted: dict[str, str],
) -> Chosen:
    """The set ROOT will resolve, built here with the same function.

    A preview that read all nine manifests out of the `--root` working
    trees would say `6 contracts satisfied` where root, which reads only
    the components that are trees under an install root, refuses with
    `the set provides it nowhere`. A preview that promises what root
    refuses is worse than no preview.

    So the decision is `live_state.select_manifests`, one function, over
    the same `trees`. The only thing that differs is where "the manifest at
    a commit" comes from: root clones at a verified tag, and this side
    reads the working tree the caller named — which is the same file, and
    which is why this side says `local:` and never `safe:`.

    `--state <file>` resolves every manifest on purpose. It is a fixture handed
    to the pure resolver, there are no install trees behind it, and
    selecting against an empty walk would silence every component.
    """
    found = manifests.manifests()
    if args.state is not None:
        return Chosen(manifests=found)

    return select_manifests(deploying_names(state, wanted), built.trees, found.get)


@dataclass(frozen=True)
class _Resolved:
    """One fetch, and everything read and resolved after it."""

    fetched: CorpusReport | None
    built: BuiltState
    chosen: Chosen
    resolution: Resolution


def _run_request(args: argparse.Namespace) -> int:
    """§2.3: file ONE request, after printing what the phone will show."""
    wanted = _wanted(args.components)
    if args.wait is not None:
        # The executor's own parser, BEFORE the wait: a malformed version
        # would otherwise sit out the whole deadline and only then refuse.
        _plan(args, wanted)

    found = _poll(args, wanted)
    fetched, chosen, resolution = found.fetched, found.chosen, found.resolution
    state = found.built.state
    _print_notes(found.built)
    for item in resolution.unprovided:
        print(f"note: {item.line()}", file=sys.stderr)

    _refuse_unmoved(args, found, wanted)
    request = _plan(args, wanted)
    view = preview_of(
        resolution,
        chosen,
        args.requested_by,
        _local_hash(args, resolution, state, request.id),
    )
    filed = None if args.dry_run else file_request(request, args.spool)
    if args.json:
        report = {
            "ok": True,
            "id": request.id,
            "filed": filed,
            "corpus": fetched.lines if fetched else [],
            "phone": view.summary.as_dict(),
        }
        print(json.dumps(report, indent=2))

        return EXIT_OK

    print(f"request {request.id}")
    print(HEADER)
    for line in view.lines():
        print(line)

    if filed is None:
        _print_corpus(fetched)
        print("\ndry run: nothing written")

        return EXIT_OK

    _print_corpus(fetched)
    print(f"\nfiled: {filed}")
    print("the executor drains it on the next path activation (§2.2)")

    return EXIT_OK


def _poll(args: argparse.Namespace, wanted: dict[str, str]) -> _Resolved:
    """Fetch, then resolve. `--wait` repeats both every `POLL_S` seconds
    until every named component would move or the deadline passes, and
    answers the last poll either way: `_refuse_unmoved` refuses a late one.

    A fetch brings only what GitHub already has, and a tag lands there
    about four minutes after its merge: a request filed inside them
    is refused. The wait is those minutes. `monotonic` and `sleep` are this
    module's own names, so a test
    swaps them the way it swaps `refresh`, and never sleeps.
    """
    started = monotonic()
    # The fetch comes FIRST. The corpus is refreshed hourly and this side
    # reads its tags, so a preview built before the fetch would say `noticeboard`
    # has no tag minutes after CI made one, and a ROOT read before the
    # fetch can miss a component.yaml that merged minutes ago the same way.
    # `wanted` needs no root, so it comes first and costs nothing.
    #
    # A dry run fetches nothing unless it waits: a wait that never fetched
    # would see only the hourly corpus sync.
    fetched = None if args.dry_run and args.wait is None else _refresh_corpus(args, wanted)
    found = _resolve_after(args, wanted, fetched)
    while args.wait is not None and (unmoved := _unmoved(found, wanted)):
        elapsed = monotonic() - started
        if elapsed >= args.wait:
            break

        # One line per poll, on stderr like every note, after whatever a
        # failed fetch said. `FETCH_NOTE` already said what the fetch is for.
        _note_failed(found.fetched)
        line = WAITING.format(names=", ".join(unmoved), elapsed=int(elapsed), limit=args.wait)
        print(line, file=sys.stderr, flush=True)
        sleep(min(POLL_S, args.wait - elapsed))
        found = _resolve_after(args, wanted, refresh(repos_of(wanted), Path(args.corpus)))

    return found


def _note_failed(report: CorpusReport | None) -> None:
    """A fetch that failed, said on the poll it failed. Without it a broken
    fetch, or a run as root, reads as a tag that never came, for the whole
    deadline."""
    if report is None or report.ok:
        return

    for line in report.lines:
        print(f"note: {line}", file=sys.stderr)


def _resolve_after(
    args: argparse.Namespace, wanted: dict[str, str], fetched: CorpusReport | None
) -> _Resolved:
    """The roots, the live state and the resolution, all read after `fetched`."""
    manifests = _found(args, fetched)
    built = _request_state(args, wanted)
    chosen = _chosen_manifests(args, manifests, built, built.state, wanted)
    try:
        resolution = resolve(chosen.manifests, built.state, wanted)
    except Refusal:
        # A refusal ends the command on any poll, so the notes of the state
        # it refused print first, where they always have.
        _print_notes(built)
        raise

    return _Resolved(fetched, built, chosen, resolution)


def _unmoved(found: _Resolved, wanted: dict[str, str]) -> list[str]:
    """The named components this resolution would not move, by name.

    `NAME` moves once its newest tag is not the live version. `NAME@VERSION`
    also needs exactly that tag in the corpus: that is when the builder can
    name its commit in `facts`.
    """
    deploying = found.resolution.deploying
    facts = found.built.state.facts

    return [
        name
        for name in sorted(wanted)
        if name not in deploying or (wanted[name] != LATEST and name not in facts)
    ]


def _refuse_unmoved(args: argparse.Namespace, found: _Resolved, wanted: dict[str, str]) -> None:
    """Refuse a request that would not move what it names.

    Without `--wait` the rule is unchanged: refused only when nothing moves.
    With it, every named component has to move, because that is what the
    wait was for, and the refusal says how long it waited.
    """
    order = found.resolution.order
    if args.wait is None and not order:
        raise Refusal(RefusalCode.REQUEST, "request", NOTHING_DEPLOYS)

    if args.wait is None:
        return

    unmoved = _unmoved(found, wanted)
    if not unmoved:
        return

    detail = NO_TAG_MOVES.format(names=", ".join(unmoved)) if order else NOTHING_DEPLOYS
    raise Refusal(RefusalCode.REQUEST, "request", f"{detail} {WAITED.format(limit=args.wait)}")


def _plan(args: argparse.Namespace, wanted: dict[str, str]):
    """§2.3's body, judged by the executor's own parser (`requester/file.py`).

    No return annotation: the type is `Request`, which lives in `executor/`,
    and this module never imports `executor/` (`handover/AGENTS.md`).
    """
    return plan_request(
        wanted,
        requested_by=args.requested_by,
        now=time.time(),
        kind="rollback" if args.rollback_of else "release",
        rollback_of=args.rollback_of,
    )


def _refresh_corpus(args: argparse.Namespace, wanted: dict[str, str]) -> CorpusReport:
    """Bring `/srv/agents/code/<repo>` to the commit root will look for.

    Root reads the release source from the corpus and nothing else
    (`executor/source.py`), the corpus is refreshed hourly, and this command
    runs minutes after a merge. This is the one place in the whole package
    that can fix that, because it is the one that runs as the operator.

    It runs BEFORE the preview: this side reads the corpus' own tags, so a
    preview built first would say `noticeboard` has no tag minutes after CI made
    one.
    """
    # stderr, so `--json` still prints one parseable object. It goes out
    # BEFORE the fetch, because the fetch is the slow part of this command
    # and a reader should know what it is waiting for.
    print(FETCH_NOTE.format(root=args.corpus), file=sys.stderr, flush=True)

    return refresh(repos_of(wanted), Path(args.corpus))


def _print_corpus(report: CorpusReport | None) -> None:
    """Every line the fetch produced, and one warning if any of them failed.

    A failed fetch never stops the request: the corpus being behind is a
    release root refuses with a reason, and refusing to FILE here would turn
    a network blip into a command the operator cannot run at all.
    """
    if report is None:
        return

    for line in report.lines:
        print(f"  {line}")

    if not report.ok:
        print(f"  {BEHIND_NOTE}")


def _set_text(moving: dict[str, str]) -> str:
    return ", ".join(f"{name} {version}" for name, version in sorted(moving.items()))


def _run_follow(args: argparse.Namespace) -> int:
    """One run of the timer: fetch, read what a newer tag moves, and file
    one request for a set that is new (`follow/`, rules 2 to 6)."""
    wanted = dict.fromkeys(args.components, LATEST)
    corpus = Path(args.corpus)
    # A failed fetch is a printed line, and the run reads the corpus as it
    # is: the next firing fetches again.
    _note_failed(refresh(repos_of(wanted), corpus))
    built = build_state(tuple(_install_roots(args)), wanted, local_readers(corpus))
    moving, notes = moving_of(built.state, wanted)
    for line in notes:
        print(f"note: {line}", file=sys.stderr)

    marker_file = Path(args.marker) if args.marker else default_marker()
    marker = read_marker(marker_file)
    now = wall_time()
    answer = Answer.NONE
    if moving and moving == marker.asked:
        answer = read_answer(done_dir_of(args.spool), marker.request_id)

    step = decide(moving, marker, answer, now)
    if step is Step.IDLE:
        print("nothing new")

        return EXIT_OK

    if step is Step.HELD:
        print(f"held: {_set_text(moving)} is asked already, as {marker.request_id}")

        return EXIT_OK

    if step is Step.SEEN:
        if not args.dry_run:
            write_marker(marker_file, seen_marker(moving, marker))

        print(f"seen: {_set_text(moving)}. The next run files it if it reads the same set")

        return EXIT_OK

    request = plan_request(moving, requested_by=FOLLOWED_BY, now=now)
    if args.dry_run:
        print(f"dry run: would file {_set_text(moving)}")

        return EXIT_OK

    # The marker FIRST (`follow/`, rule 6).
    write_marker(marker_file, asked_marker(moving, marker, request.id, now))
    filed = file_request(request, args.spool)
    print(f"filed: {_set_text(moving)} as {filed}")

    return EXIT_OK


def _add_shared(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", action="append", help="repository root to search (repeatable)")
    parser.add_argument("--json", action="store_true", help="one JSON object instead of a table")
    parser.add_argument(
        "--partial",
        action="store_true",
        help="accept roots that hold only some of contract 06 §1's components",
    )


def _seconds(text: str) -> int:
    """`--wait SECONDS`: whole seconds. Anything else is argparse's usage
    error, exit 2, before anything is fetched."""
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError(f"expected whole seconds, read {safe_token(text)}")

    return int(text)


def _add_request(ask: argparse.ArgumentParser) -> None:
    """§2.3's one writer. Every option here narrows what is written; none
    of them can add a field, because §2.3 has exactly seven."""
    ask.add_argument("components", nargs="+", metavar="NAME[@VERSION]")
    _add_shared(ask)
    ask.add_argument(
        "--state",
        help="resolve against this document instead of building one (a fixture, not the host)",
    )
    ask.add_argument(
        "--install-root",
        dest="install_root",
        action="append",
        help="where components install; repeatable, defaults to the executor's own two",
    )
    ask.add_argument(
        "--corpus",
        default=CORPUS_ROOT,
        help="the clones this side reads tags out of; root re-reads them from GitHub",
    )
    ask.add_argument("--requested-by", dest="requested_by", default=HUMAN)
    ask.add_argument("--rollback-of", dest="rollback_of", help="undo this release id")
    ask.add_argument("--spool", default=REQUESTS_PATH, help="the requests directory")
    ask.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="print and write nothing (with --wait it still fetches, and files nothing)",
    )
    ask.add_argument(
        "--wait",
        nargs="?",
        const=DEFAULT_WAIT_S,
        type=_seconds,
        metavar="SECONDS",
        help=f"fetch and resolve every {POLL_S}s until every named component would move, "
        f"then file. Refuse after SECONDS (default {DEFAULT_WAIT_S})",
    )
    ask.set_defaults(handler=_run_request)


def _add_follow(watch: argparse.ArgumentParser) -> None:
    """`request` on a timer. It takes names and no version: the version is
    the newest tag, and that is the whole point."""
    watch.add_argument("components", nargs="+", metavar="NAME", choices=releasable_names())
    watch.add_argument(
        "--install-root",
        dest="install_root",
        action="append",
        help="where components install; repeatable, defaults to the executor's own two",
    )
    watch.add_argument(
        "--corpus",
        default=CORPUS_ROOT,
        help="the clones this side fetches and reads tags out of",
    )
    watch.add_argument("--spool", default=REQUESTS_PATH, help="the requests directory")
    watch.add_argument(
        "--marker",
        help="where this command remembers the set it asked for (default: under the home)",
    )
    watch.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="fetch, print the step, and write nothing",
    )
    # `main` reads it for every subcommand.
    watch.set_defaults(handler=_run_follow, json=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="the rework release command")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate every component.yaml on disk")
    _add_shared(check)
    check.add_argument(
        "--site",
        action="store_true",
        help="also require every site value a release reads (the executor's verify hook)",
    )
    check.set_defaults(handler=_run_check)

    resolver = sub.add_parser("resolve", help="say what a release would do")
    resolver.add_argument("components", nargs="*", metavar="NAME=VERSION")
    _add_shared(resolver)
    resolver.add_argument("--state", help="live-state document (JSON)")
    resolver.add_argument("--id", help="release id: 26 upper-case Crockford characters")
    resolver.add_argument("--requested-by", dest="requested_by", default=DEFAULT_REQUESTED_BY)
    resolver.add_argument(
        "--resolved-at", dest="resolved_at", type=float, default=DEFAULT_RESOLVED_AT
    )
    resolver.set_defaults(handler=_run_resolve)

    tags = sub.add_parser("allocate-tags", help="what tag this merge allocates (contract 06 §2.1)")
    tags.add_argument(
        "--paths",
        help="file of changed repo-relative paths, one per line, each measured "
        "from the newest tag of the component it belongs to (contract 06 §2.1)",
    )
    tags.add_argument("--tags", help="file of existing tags, one per line")
    tags.add_argument("--at-sha", dest="at_sha", help="file of tags already on this commit")
    tags.add_argument(
        "--levels",
        help="file of `<component><TAB><label><TAB><path>` lines: a merged pull "
        "request in that component's range carried the label and changed the path",
    )
    tags.add_argument(
        "--cut",
        help="file of changed repo-relative paths, one per line: print each one "
        "cut to the prefix that decides its components, and plan nothing",
    )
    repos = [str(item) for item in Repo]
    tags.add_argument("--repo", default=str(Repo.AGENT_CONTROL), choices=repos)
    tags.add_argument("--json", action="store_true", help="one JSON object instead of a table")
    tags.set_defaults(handler=_run_allocate)

    _add_request(sub.add_parser("request", help="file one release request (§2.3)"))
    _add_follow(sub.add_parser("follow", help="file a request for every newer tag, once"))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    try:
        return handler(args)
    except Refusal as refusal:
        if args.json:
            print(json.dumps({"ok": False, **refusal.as_dict()}, indent=2))
        else:
            print(refusal.as_line(), file=sys.stderr)

        return EXIT_REFUSED
    except RequesterError as error:
        # Not a refusal: the request was fine and this host cannot file it.
        print(f"{PROG}: {error}", file=sys.stderr)

        return EXIT_USAGE
    except OSError as error:
        print(f"{PROG}: {error.strerror or 'cannot read'}", file=sys.stderr)

        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
