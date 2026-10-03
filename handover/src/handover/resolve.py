"""The resolver: a request plus the live state becomes contract 06 §9.

Two layers, deliberately separate.

1. `resolve()` decides what this release does: the action per component, the
   deploy order, and the contract table. It needs no source facts, so a
   fixture repo alone can prove every refusal.
2. `build_document()` turns that decision into contract 06 §9's resolved
   manifest and hashes it. The hash is what the operator's phone approval binds to
   (stage7-releases.md §2.5), so it is computed over canonical JSON: sorted
   keys, no whitespace, and every list in an order this module fixes.

Nothing here touches a host, a git remote or an OCI registry. Root does that
in `executor/` and hands the answers in as `ReleaseState`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .catalog import (
    CATALOG,
    CATALOG_BY_NAME,
    MANIFEST_CONTRACT_MAJOR,
    MANIFEST_CONTRACT_MINOR,
    MAX_REQUEST_COMPONENTS,
    Action,
    Releases,
)
from .contracts import ContractRow, Unprovided, build_table
from .errors import Refusal, RefusalCode, safe_token
from .manifest import ComponentManifest
from .order import build_graph, deploy_order
from .state import VERSION_RE, ReleaseState, SourceFacts

#: What a request writes instead of a number when it means "the newest
#: released version" (stage7-releases.md §2.3).
LATEST = "latest"

#: `<component>=<version>`, the one shape a request argument takes.
REQUEST_ITEM_RE = re.compile(r"([a-z][a-z0-9-]{1,30})=([0-9a-zA-Z.]{1,32})")

#: Contract 06 §9: upper-case Crockford base32, as contract 02 §2 fixes it.
ULID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

#: Contract 06 §9's `requested_by`: a family name, `human` or `ci`.
REQUESTED_BY_RE = re.compile(r"[a-z][a-z0-9-]{1,30}")

TAG_FORMAT = "{name}-v{version}"
DIGEST_PREFIX = "sha256:"
MANIFEST_VERSION = f"{MANIFEST_CONTRACT_MAJOR}.{MANIFEST_CONTRACT_MINOR}"


@dataclass(frozen=True)
class ResolvedComponent:
    """One row of contract 06 §9's `components` list, before its facts."""

    name: str
    action: Action
    from_version: str | None
    to_version: str | None

    def tag(self) -> str | None:
        if self.to_version is None:
            return None

        return TAG_FORMAT.format(name=self.name, version=self.to_version)


@dataclass(frozen=True)
class Resolution:
    """What this release would do. Contract 06 §9 minus the source facts."""

    components: tuple[ResolvedComponent, ...]
    order: tuple[str, ...]
    contracts: tuple[ContractRow, ...]
    deploying: frozenset[str]
    #: Every `requires` entry whose contract has no provider in the set
    #: (contract 06 §3.2, C1's second half). Reported, never refused: root
    #: cannot tell "nothing provides it" from "its provider is not a tree
    #: under an install root". Both halves of the system read this, so the
    #: preview and the phone say the same thing.
    unprovided: tuple[Unprovided, ...] = ()


def parse_request(items: list[str]) -> dict[str, str]:
    """`['chaperone=2.1.0', 'attendance=latest']` to a map, refusing anything else."""
    if len(items) > MAX_REQUEST_COMPONENTS:
        detail = f"names more than {MAX_REQUEST_COMPONENTS} components"
        raise Refusal(RefusalCode.REQUEST, "request", detail)

    request: dict[str, str] = {}
    for item in items:
        matched = REQUEST_ITEM_RE.fullmatch(item)
        if matched is None:
            detail = f"expected <component>=<version>, read {safe_token(item)}"
            raise Refusal(RefusalCode.REQUEST, "request", detail)

        name, version = matched.group(1), matched.group(2)
        if name in request:
            raise Refusal(RefusalCode.REQUEST, "request", f"names {name} twice")

        if version != LATEST and not VERSION_RE.fullmatch(version):
            detail = f"{name} wants MAJOR.MINOR.PATCH or '{LATEST}'"
            raise Refusal(RefusalCode.REQUEST, "request", detail)

        request[name] = version

    return request


def _check_requested(name: str, manifests: dict[str, ComponentManifest]) -> None:
    """A request may name a releasable component this repo set declares."""
    row = CATALOG_BY_NAME.get(name)
    if row is None:
        detail = f"{safe_token(name)} is not a component contract 06 §1 lists"
        raise Refusal(RefusalCode.REQUEST, "request", detail)

    if row.releases is Releases.NO:
        raise Refusal(RefusalCode.REQUEST, "request", f"{name} never releases (contract 06 §6)")

    if name not in manifests:
        detail = f"{name} has no component.yaml under any root"
        raise Refusal(RefusalCode.REQUEST, "request", detail)


def _target_version(name: str, wanted: str, state: ReleaseState) -> str:
    """Turn `latest` into a number, or refuse: the resolver reads no tags."""
    if wanted != LATEST:
        return wanted

    newest = state.latest.get(name)
    if newest is None:
        detail = f"{name}={LATEST} names no released tag: name a version instead"
        raise Refusal(RefusalCode.REQUEST, "request", detail)

    return newest


def _one_component(name: str, request: dict[str, str], state: ReleaseState) -> ResolvedComponent:
    """Contract 06 §9's action, from_version and to_version for one row."""
    live = state.live.get(name)
    if CATALOG_BY_NAME[name].releases is Releases.NO:
        # Contract 06 §9.1: a data component carries no version and no tag.
        return ResolvedComponent(name, Action.UNCHANGED, None, None)

    wanted = request.get(name)
    if wanted is None:
        return ResolvedComponent(name, Action.UNCHANGED, live, live)

    target = _target_version(name, wanted, state)
    # A request that names the version already live changes nothing. Saying
    # `unchanged` keeps step 9 from restarting a unit for no reason.
    action = Action.UNCHANGED if target == live else Action.DEPLOY

    return ResolvedComponent(name, action, live, target)


def deploying_versions(state: ReleaseState, request: dict[str, str]) -> dict[str, str]:
    """Which components this request would MOVE, and to what version.

    `resolve` needs a manifest per component, and the executor needs to know
    what deploys before it decides which manifests it may believe (contract
    06 §9, `stage7-releases.md` §6 row 20). It also needs the target version
    before it can name a tag, which is what builds contract 06 §11's
    `facts`. The action depends only on the request and the live state, so
    this is that decision on its own.

    A name the catalog does not list is skipped rather than refused: the
    refusal belongs to `resolve`, which names the rule it broke. So is a
    name whose `latest` the live state cannot resolve: the same `resolve`
    call refuses it with the reason.
    """
    found: dict[str, str] = {}
    for name in sorted(request):
        if name not in CATALOG_BY_NAME:
            continue

        try:
            row = _one_component(name, request, state)
        except Refusal:
            continue

        if row.action is Action.DEPLOY and row.to_version is not None:
            found[name] = row.to_version

    return found


def deploying_names(state: ReleaseState, request: dict[str, str]) -> frozenset[str]:
    """`deploying_versions` when only the names are wanted."""
    return frozenset(deploying_versions(state, request))


def resolve(
    manifests: dict[str, ComponentManifest],
    state: ReleaseState,
    request: dict[str, str],
) -> Resolution:
    """Decide what this release does, or refuse the set with one report."""
    for name in sorted(request):
        _check_requested(name, manifests)

    components = tuple(_one_component(row.name, request, state) for row in CATALOG)
    ordered = tuple(sorted(components, key=lambda item: item.name))
    deploying = frozenset(item.name for item in ordered if item.action is Action.DEPLOY)

    order = deploy_order(build_graph(manifests), deploying)
    table = build_table(manifests, deploying, state.provided)

    return Resolution(
        components=ordered,
        order=order,
        contracts=table.rows,
        deploying=deploying,
        unprovided=table.unprovided,
    )


#: §9's source columns for a component root neither deploys nor fetched.
#: They are `null` rather than absent, because a reader of the ledger must
#: be able to tell "root did not look at this one" from "root forgot".
NO_FACTS = SourceFacts(sha=None, input_digest=None, artifact_digest=None)


def _component_entry(item: ResolvedComponent, facts: dict[str, SourceFacts]) -> dict[str, object]:
    known = facts.get(item.name, NO_FACTS)

    return {
        "name": item.name,
        "action": str(item.action),
        "from_version": item.from_version,
        "to_version": item.to_version,
        "tag": item.tag(),
        "sha": known.sha,
        "input_digest": known.input_digest,
        "artifact_digest": known.artifact_digest,
    }


def _contract_entry(row: ContractRow) -> dict[str, object]:
    return {
        "contract": str(row.contract),
        "provider": row.provider,
        "version": row.version(),
        "consumers": [
            {"name": item.name, "major": item.major, "min_minor": item.min_minor}
            for item in row.consumers
        ],
    }


def canonical_json(document: dict[str, object]) -> str:
    """Contract 06 §9's hash input: sorted keys, no whitespace."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_hash(document: dict[str, object]) -> str:
    """`sha256:<64 hex>` over the canonical JSON of every other field."""
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()

    return f"{DIGEST_PREFIX}{digest}"


def build_document(
    resolution: Resolution,
    state: ReleaseState,
    release_id: str,
    requested_by: str,
    resolved_at: float,
) -> dict[str, object]:
    """Contract 06 §9's resolved manifest, `manifest_sha256` included."""
    if not ULID_RE.fullmatch(release_id):
        detail = f"id must be 26 upper-case Crockford characters: {safe_token(release_id)}"
        raise Refusal(RefusalCode.REQUEST, "request", detail)

    if not REQUESTED_BY_RE.fullmatch(requested_by):
        detail = f"requested_by must be a family name, 'human' or 'ci': {safe_token(requested_by)}"
        raise Refusal(RefusalCode.REQUEST, "request", detail)

    document: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "id": release_id,
        "resolved_at": resolved_at,
        "requested_by": requested_by,
        "components": [_component_entry(item, state.facts) for item in resolution.components],
        "order": list(resolution.order),
        "contracts": [_contract_entry(row) for row in resolution.contracts],
    }
    document["manifest_sha256"] = manifest_hash(document)

    return document
