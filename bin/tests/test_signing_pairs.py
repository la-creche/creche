"""The list of signing pairs of the release signer covers the catalog.

Section 4.3 item 6 of `docs/design/forgejo-release-path.md` holds the list
of `rust/crates/ci-signer/signing-pairs.tsv` equal to
`catalog.RELEASE_WORKFLOW`. Until FR9 adds mcp and creche-release to
`RELEASE_WORKFLOW`, the test holds each catalog row in the list with the same
file. The test lives here, not in `handover/tests/`, so the change moves no
`handover` tag.
"""

from pathlib import Path

from handover.catalog import RELEASE_WORKFLOW

REPO_ROOT = Path(__file__).resolve().parents[2]
PAIRS_FILE = REPO_ROOT / "rust" / "crates" / "ci-signer" / "signing-pairs.tsv"


def read_pairs() -> dict[str, str]:
    """Each repository of the list with its workflow file."""
    pairs: dict[str, str] = {}
    for line in PAIRS_FILE.read_text(encoding="utf-8").splitlines():
        repo, workflow = line.split("\t")
        pairs[repo] = workflow
    return pairs


def test_each_catalog_row_is_a_signing_pair() -> None:
    pairs = read_pairs()
    for repo, workflow in RELEASE_WORKFLOW.items():
        assert pairs.get(repo.value) == workflow, repo


def test_registry_has_no_pair() -> None:
    # Section 4.3: `registry-data` is `Releases.NO`, so root never verifies
    # a registry tag.
    assert "registry" not in read_pairs()
