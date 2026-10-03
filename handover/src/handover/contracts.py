"""Rules C1 to C5 of contract 06 §3.2, against one resolved set.

The resolved set is every component at the version it would run at after this
release. A contract version is two integers, so a check here is arithmetic and
never a search (contract 06 §2.2).

No pin is written by hand (contract 06 §3.3): a component declares the floor
it calls at. The set either satisfies every floor or it is refused with both
versions named.

C5 has no refusal column in the contract. It is a property this module makes
true rather than a check it runs: a component with neither `provides` nor
`requires` appears in no contract row, so no other component's floor can drag
it into a set.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import CONTRACT_OWNER, ContractId
from .errors import Refusal, RefusalCode
from .manifest import ComponentManifest

#: Contract 06 §3.2, rule C5's shape applied to C1: a requirement root
#: cannot check is a LINE, never a refusal. See `_check_floors`.

#: What a contract version prints as, in a refusal and in the §9 document.
VERSION_FORMAT = "{major}.{minor}"


@dataclass(frozen=True)
class Consumer:
    """One `requires` entry, from the component that wrote it."""

    name: str
    major: int
    min_minor: int


@dataclass(frozen=True)
class Unprovided:
    """One `requires` entry whose contract has no provider in the set.

    Contract 06 §3.2, rule C1's second half. It is REPORTED and never
    refused, because root cannot tell "nothing provides it" from "its
    provider is not a tree under an install root", and `playpen`,
    an image and never a tree, is always the second.
    """

    consumer: str
    contract: ContractId
    major: int
    min_minor: int

    def line(self) -> str:
        """What the ledger records and the phone counts. It names the
        component §3 says owns the contract, because that is the thing a
        reader has to go and look at."""
        floor = VERSION_FORMAT.format(major=self.major, minor=self.min_minor)
        owner = CONTRACT_OWNER[self.contract]

        return (
            f"not verified: {self.consumer} requires {self.contract} {floor}, "
            f"and {owner} is not a tree under an install root"
        )


@dataclass(frozen=True)
class ContractRow:
    """One row of contract 06 §9's `contracts` table."""

    contract: ContractId
    provider: str
    major: int
    minor: int
    consumers: tuple[Consumer, ...]

    def version(self) -> str:
        return VERSION_FORMAT.format(major=self.major, minor=self.minor)


@dataclass(frozen=True)
class Table:
    """What §3.2's check produced: §9's rows, and what it could not check."""

    rows: tuple[ContractRow, ...]
    unprovided: tuple[Unprovided, ...] = ()


LiveProvided = dict[ContractId, tuple[int, int]]


def _providers_of(manifests: dict[str, ComponentManifest]) -> dict[ContractId, list[str]]:
    """Every component that claims each contract, in name order."""
    claims: dict[ContractId, list[str]] = {}
    for name in sorted(manifests):
        for provided in manifests[name].provides:
            claims.setdefault(provided.contract, []).append(name)

    return claims


def _check_c2(claims: dict[ContractId, list[str]]) -> None:
    """C2: exactly one component provides each contract."""
    for contract, names in sorted(claims.items()):
        if len(names) == 1:
            continue

        detail = f"ambiguous provider for {contract}: {', '.join(names)}"
        raise Refusal(RefusalCode.C2, str(contract), detail)


def _check_c3(claims: dict[ContractId, list[str]]) -> None:
    """C3: the one provider must be the component contract 06 §3 names."""
    for contract, names in sorted(claims.items()):
        owner = CONTRACT_OWNER[contract]
        if names[0] == owner:
            continue

        detail = f"wrong provider for {contract}: {names[0]} provides it, {owner} owns it"
        raise Refusal(RefusalCode.C3, str(contract), detail)


def _consumers_of(
    manifests: dict[str, ComponentManifest], contract: ContractId
) -> tuple[Consumer, ...]:
    consumers: list[Consumer] = []
    for name in sorted(manifests):
        for required in manifests[name].requires:
            if required.contract is contract:
                consumers.append(Consumer(name, required.major, required.min_minor))

    return tuple(consumers)


def _check_c1(row: ContractRow, consumer: Consumer) -> None:
    """C1: same major, and the provider's minor at or above the floor."""
    if consumer.major == row.major and row.minor >= consumer.min_minor:
        return

    floor = VERSION_FORMAT.format(major=consumer.major, minor=consumer.min_minor)
    detail = (
        f"{consumer.name} requires {row.contract} {floor}, {row.provider} provides {row.version()}"
    )
    raise Refusal(RefusalCode.C1, str(row.contract), detail)


def _check_c4(row: ContractRow, deploying: frozenset[str], live: LiveProvided) -> None:
    """C4: a major bump ships with every consumer of that contract.

    A contract with no live version is a first install, so there is nothing to
    bump from and the rule does not apply (contract 06 §9, `from_version`).
    """
    was = live.get(row.contract)
    if was is None or was[0] == row.major:
        return

    if row.provider not in deploying:
        return

    missing = sorted(item.name for item in row.consumers if item.name not in deploying)
    if not missing:
        return

    detail = (
        f"breaking change needs a set: {row.contract} {was[0]}.{was[1]} -> {row.version()}, "
        f"missing consumers: {', '.join(missing)}"
    )
    raise Refusal(RefusalCode.C4, str(row.contract), detail)


def build_table(
    manifests: dict[str, ComponentManifest],
    deploying: frozenset[str],
    live: LiveProvided,
) -> Table:
    """Check C1 to C4 over the resolved set and return contract 06 §9's table.

    Rows come back in contract-id order. The order is fixed here and not left
    to the caller, because the table is hashed (contract 06 §9).
    """
    claims = _providers_of(manifests)
    _check_c2(claims)
    _check_c3(claims)

    rows: list[ContractRow] = []
    for contract in sorted(claims):
        provider = manifests[claims[contract][0]]
        provided = next(item for item in provider.provides if item.contract is contract)
        rows.append(
            ContractRow(
                contract=contract,
                provider=provider.name,
                major=provided.major,
                minor=provided.minor,
                consumers=_consumers_of(manifests, contract),
            )
        )

    # CONTRACT-QUESTION (contract 06 §3.2): the table numbers the rules but
    # fixes no evaluation order. C4 runs first because a major bump breaks
    # every consumer's floor as well, and only C4's message says what to do
    # about it — add the missing consumers to the set. C1 still runs after.
    for row in rows:
        _check_c4(row, deploying, live)

    return Table(rows=tuple(rows), unprovided=_check_floors(manifests, rows))


def _check_floors(
    manifests: dict[str, ComponentManifest], rows: list[ContractRow]
) -> tuple[Unprovided, ...]:
    """C1 over every `requires` entry the set can answer.

    **A contract with no provider in the set is reported, not refused.**
    Root learns a component's interface from a tree under an install root,
    and a provider can be live without one: `playpen` is an image
    and never a tree. Refusing would answer `caregiver requires channel
    0.11, the set provides it nowhere` for a set the host already runs.

    Root cannot tell "nothing provides it" from "its provider is not a tree
    root reads". Refusing there refuses every release on this host for ever,
    and refuses for a reason the release does not cause and cannot fix. So
    the honest answer is the one §2.5 already has words for: name it, count
    it as `suspect`, and let the operator read it before the tap.

    The teeth stay where root can bite. A provider that IS in the set is
    checked exactly as before, which covers every component a release
    manages — and a release can only break a contract it can see.
    """
    by_contract = {row.contract: row for row in rows}
    missing: list[Unprovided] = []
    for name in sorted(manifests):
        for required in manifests[name].requires:
            row = by_contract.get(required.contract)
            if row is None:
                missing.append(
                    Unprovided(name, required.contract, required.major, required.min_minor)
                )
                continue

            _check_c1(row, Consumer(name, required.major, required.min_minor))

    return tuple(missing)
