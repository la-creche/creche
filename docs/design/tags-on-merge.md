# Tags on merge for chaperone and registry

> Status as of 2026-11-14: Done. The three pull requests of the header merged (mcp, chaperone and registry).
> Host names, addresses and accounts are placeholders. The operator's private notes hold the real values.

Date: 2026-10-31. Status: decided 2026-10-31. The operator chose §2 for chaperone
and R-A for registry. PRs: la-creche/mcp#91, la-creche/chaperone#80,
la-creche/registry#52.

The operator's goal: every la-creche repository mints its tags the way `creche`
does. `private-docs` is out of scope (the operator, 2026-10-31). `mcp` is done in
la-creche/mcp#91.

## 1. How creche mints a tag

1. A merge to `main` runs `release.yml`: the gate again, then one job that
   may write.
2. `handover/bin/allocate-tags.sh` picks each changed component's next
   `<component>-vX.Y.Z`: the newest tag of that component, plus a patch, or
   the level of a `bump:minor` / `bump:major` label on the merged PR.
3. No version lives in a tracked file (contract 06 §2: "The tag is the
   version. The build stamps the version into the artifact.").
4. `creche-follow` on the host files a release request for each new tag of a
   component with `Releases.YES`. The operator taps once.

## 2. chaperone

### Today

- `release.yml` tags `chaperone-vX.Y.Z` only when a PR changes `version` in
  `crates/chaperone/Cargo.toml` (`chaperone/docs/design.md`:586). Every
  release needs a version-bump PR, such as chaperone#79.
- The run builds a musl archive, signs it with cosign (`sign` job, keyless,
  identity `release.yml@refs/heads/main`), then creates the tag and the
  Release with both assets.
- The host installs only that signed archive. It never builds chaperone
  (`handover/executor/install.py`:79-83, check P6 in `provenance.py`).
- `chaperone --version` prints the `Cargo.toml` version (clap `version`).
  Nothing on the host compares it with the tag: The host reads its own
  `.release-version` stamp.

### Proposal

`release.yml` keeps its jobs, in this order:

1. `plan`: run `allocate-tags.sh --dry-run` with `TAG_REPO=chaperone`, from
   creche checked out at a pinned `CRECHE_REF` (the mcp pattern). Its
   output is the next tag, or nothing.
2. `build`: stamp the planned version into the working tree's
   `Cargo.toml` (not committed), then build and pack as today.
   `chaperone --version` then matches the tag.
3. `sign`: unchanged.
4. `release`: run `allocate-tags.sh` for real, with `ASSET_DIR` holding the
   archive and its bundle, and `COSIGN`. The script already supports a
   binary component this way (`allocate-tags.sh`:376-453). It verifies the
   signature, creates the tag and the Release. A check refuses a tag that
   differs from the planned one. The `release` concurrency group already
   stops two runs from racing.

`crates/chaperone/Cargo.toml` keeps `version = "0.0.0"` as a placeholder,
because Cargo needs the field. `docs/design.md` §12 says the merge tags.

Cost: about 3 PRs in chaperone (workflow, placeholder plus docs, test of the
stamp) and maybe 1 in creche, if the allocator needs a flag to print the
planned tag alone. No host change.

## 3. registry

### Today

- No tags. `creche-registry-sync.timer` fast-forwards `/srv/agents/registry`
  to `origin/main` every minute. `caregiver` applies families with no tap.
- This is deliberate. Contract 06 §6: "`registry-data` never takes a
  release". stage7 §5.3: "Registry data applies with no tap because the operator
  merges it".
- creche's catalog holds `registry-data` as `Kind.DATA`, `Releases.NO`. The
  executor has no way to install a data component.
- `noticeboard` commits family edits locally into `/srv/agents/registry`.

### Option R-A: tags only (recommended)

The registry gets a copy of mcp's `rework-component-tags.yml`:
`TAG_REPO=registry`, a gate job that runs `validate.yml`'s validator, then
the allocator. Each merge mints `registry-data-vX.Y.Z`. Nothing on the host
changes: the sync timer still applies `main`, and no tap is asked, because
`registry-data` stays `Releases.NO`.

Result: every registry state that reached the host has a tag, which gives a
name to roll back to and to quote in a report. Cost: 1 PR in registry.

### Option R-B: tags and a tapped release

The tag becomes the only way a registry change reaches the host.

1. creche: a new executor path for `Kind.DATA` (check out the tag into
   `install.to`, swap with `.prev`); `registry-data` becomes
   `Releases.YES`; the follower lists it.
2. Host: remove `creche-registry-sync`, the watchdog's fifth check and its
   wiring. Repoint `caregiver`, `door-trigger` and `noticeboard`, or keep a
   checkout at the tag.
3. `noticeboard`'s local commits need a new home.
4. Contract 06 §6 and stage7 §5.3 change.

Result: every family change waits for a tap, which closes the "a registry
merge applies with no tap" gap. Cost: about 8 to 10 PRs across creche,
registry and private-docs, plus a host window.

## 4. Decisions for the operator

1. chaperone: build the proposal in §2? (Recommended: yes.)
2. registry: R-A (tags only) or R-B (tapped release)? (Recommended: R-A.
   R-B can follow later on top of R-A's tags.)
