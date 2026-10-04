"""Steps 8 to 10 for ONE component: stage, switch, verify, restore.

`stage7-releases.md` §2.4 rows 8, 9 and 10, and contract 06 §4 and §5.

```
  fetch <sha>            <install.to>.new          <install.to>
  into the work tree  ->  built, never live    ->   swapped, unit restarted
                                                     |  verify fails
                          <install.to>.prev   <------'  put back, verify again
```

Seven rules this module exists to keep.

1. **Nothing is swapped until everything is built.** A failed `stage`
   removes the `.new` tree and nothing on the host has changed (§2.4 row 8).
2. **A path is checked for containment before it is touched.** `install.to`
   and `install.prev` are validated absolute paths, and here `install.to`
   must also be a DIRECT child of a root the executor was configured with,
   and `install.prev` must be `<install.to>.prev`. A manifest that a
   merged pull request could widen must not be able to name `/etc`, the
   root itself (a swap would rename every installed tree away), or another
   component's tree (a swap removes `prev` first).
3. **No symlink is followed out of the staged tree.** The verify hook's own
   `argv[0]` is resolved inside `<install.to>.new` before the switch, so a
   staged tree cannot point the hook at a binary somewhere else.
4. **A unit file is refreshed IN PLACE, and only where one is already
   installed.** A release never runs an installer script: `bootstrap-root.sh`
   is a second deploy, and re-running the site's bootstrap script overwrites the
   UI's access key. An uninstalled unit is listed under `manual`.
5. **A staged tree is readable by everyone and writable by root alone,
   whatever the release unit's own umask.** `normalize_modes` runs once
   staging finishes, so a `UMask=` this module does not control never
   decides whether the user a unit runs as can read what it just started.
6. **The unit a switch replaces is kept first, beside itself, and a
   restore puts it back before the previous tree restarts.**
   The unit file is part of the release artifact, so a failed verify must
   not restart the previous tree under this release's unit. Both copies
   run as the owner of the unit's directory, the identity the refresh
   already uses.

       <unit>       --keep------->  <unit>.prev   before the switch note
       staged unit  --refresh---->  <unit>        after the swap
       <unit>.prev  --put back--->  <unit>        verify failed: then restart
7. **The unit file travels in the artifact, and is installed from the LIVE
   tree.** The clone is `root:root 0750` under the release
   unit's `UMask=0027`, and a user unit is installed as the operator, who cannot
   read it. Root installing it for them would make root their deputy
   (`handover/AGENTS.md` rule 5a). So root copies it into the staged tree,
   rule 5 makes it readable, and the unit's owner installs their own file.

       <clone>/systemd/<unit>  --stage, root-->  <install.to>.new/systemd/<unit>
       <install.to>/systemd/<unit>  --refresh, owner-->  <unit>
8. **A component's sibling units travel with its own unit.** A sibling is a
   unit file that runs out of the same tree: `attendance`'s three door units.
   A file is one when ALL of these hold.

   1. The component is `kind: venv` or `kind: binary`, names a unit, and
      that unit is installed.
   2. The file is a regular `*.service`, not a symlink, in the SAME unit
      directory, and is not the component's own unit.
   3. The INSTALLED file starts the tree, by `check_unit_binds`' test.
   4. The release carries a file of that name, and it starts the tree too.

   Each sibling is staged, kept, refreshed, put back and restarted with
   the component's own unit, by rules 4, 6 and 7. One that runs restarts
   whether its file changed or not, because the tree under it did. Nothing
   is installed that is not installed already. Three security facts hold.

   - A user unit directory is the operator's. Root never lists it: it
     opens only the names the release carries, to parse `ExecStart=`, and
     follows no symlink. Every write there runs as the operator, never as
     root.
   - In the system unit directory root writes only over a file root
     already installed there, from a tree built out of a provenance-checked
     clone.
   - The sibling set is recomputed from the tree and the host at each step.
     What goes BACK is only what the switch note or this run kept.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from ..catalog import Kind, RestoreMode, VerifyUser
from ..errors import Refusal, RefusalCode, safe_token
from ..manifest import ComponentManifest
from ..site import operator_home, operator_user
from .host import As, Command, Host, Result
from .quiet import WATCHED_SERVICES
from .selfcontained import check_tree
from .source import GIT, REFRESH_COMMAND, git_env, git_ground, require_trusted
from .spool import VerifyHook

NEW_SUFFIX: Final = ".new"

#: Rule 2: the only `install.prev` a manifest may name. `swap_in` removes
#: `prev` before the rename, so a free choice could remove any tree.
PREV_SUFFIX: Final = ".prev"

#: Where a component's unit file lives inside its repository, and where
#: every unit in this repository sits.
#: CONTRACT-QUESTION: contract 06 §8's `unit_file` defaults to this, and
#: `manifest.py` refuses the key, so no manifest can name another place.
UNIT_DIR_IN_REPO: Final = "systemd"

#: Rule 7: where the staged tree carries the unit file step 9 installs,
#: `<install.to>/systemd/<unit>`. A name this module fixes, not the
#: repository's: `stage7-releases.md` §2.4 row 8 states it.
#: CONTRACT-QUESTION: contract 06 §2 lists two files in the artifact and
#: not this third one: a row there would move the contract's minor for a
#: file no manifest can name.
UNIT_DIR_IN_TREE: Final = "systemd"

#: `uv` puts the project environment where this names, which is how a
#: `build` entry with no interpolation still lands in `<install.to>.new`.
#: CONTRACT-QUESTION: contract 06 §8's `output` defaults to this, and
#: `manifest.py` refuses the key, so every build gets this variable or
#: `BINARY_ENV_NAME`, by its kind. Reading the key would let a manifest name
#: a variable in the environment of a child that runs as root.
VENV_ENV_NAME: Final = "UV_PROJECT_ENVIRONMENT"

#: The same, for a `kind: binary` build. `cargo install` with no `--root`
#: writes each program at `<this>/bin/<name>`, so the staged tree has the
#: layout a venv has and every `ExecStart=` path keeps its shape.
#: Measured against cargo 1.92.0 on 2026-10-03, and again by
#: `handover/tests/test_handover_bin_cargo_guard.py`.
BINARY_ENV_NAME: Final = "CARGO_INSTALL_ROOT"

#: The kinds whose tree holds the programs that its units start: the
#: console scripts of a venv and the compiled programs of a binary tree,
#: both under `bin/`. A compose project's unit starts docker, and an image
#: has no unit.
PROGRAM_KINDS: Final = frozenset({Kind.VENV, Kind.BINARY})

SYSTEMCTL: Final = "/usr/bin/systemctl"
INSTALL: Final = "/usr/bin/install"

#: Absolute, like `GIT` and `SYSTEMCTL`: a child's `PATH` is built by
#: `host.child_env` and is not promised to hold whatever `uv` the caller
#: has. Contract 06 pins the same path for the manifests.
UV: Final = "/usr/local/bin/uv"

#: **A venv does not survive its own rename unless it is built to.**
#: `uv sync` writes an absolute interpreter path into every console script,
#: so after step 9 renames `<install.to>.new` to `<install.to>` each one
#: still names a directory that is gone and exits 126. Every `kind: venv`
#: component's verify hook IS a console script under `install.to`, so the
#: switch lands, the hook cannot execute, and §5.1 restores the release.
#:
#: `uv venv --relocatable` writes
#: `"$(dirname -- "$(realpath -- "$0")")"/'python'` instead, and `uv sync`
#: reuses an environment it did not create. So the environment is made
#: here, before the manifest's own `build` fills it.
#:
#: The interpreter is deliberately NOT named: run inside the component's
#: source tree, `uv venv` reads that project's `requires-python`, which is
#: the same interpreter `uv sync` would have chosen. Naming one here would
#: be a second place for a version to drift.
#:
#: Measured against uv 0.12.17 on 2026-09-20, and again by
#: `handover/tests/test_handover_r7e_relocatable.py`. `uv sync` has no
#: `--relocatable` flag and ignores `UV_VENV_RELOCATABLE`.
RELOCATABLE_ARGV: Final = (UV, "venv", "--relocatable")

UNIT_FILE_MODE: Final = "0644"

#: Rule 6: where the unit file a switch replaces is kept, beside itself.
#: systemd loads only a name that ends in a unit type, and `prev` is none.
#: CONTRACT-QUESTION: contract 06 §5 never says the unit file is part of
#: the previous artifact. It is taken to be, so a restore puts it back.
UNIT_KEPT_SUFFIX: Final = ".prev"

#: The release unit's own `UMask=`, 0027, leaves every OTHER bit at zero on
#: whatever a build writes into `<install.to>.new` — `creche-noticeboard.service`
#: runs as the operator and cannot list such a tree after the swap.
#: `normalize_modes` runs once, after the build and
#: the two stamps, and sets every mode explicitly so a staged tree is
#: readable (and, where it was already executable, runnable) by anyone, and
#: writable by root alone, whatever the unit's umask happens to be.
DIR_MODE: Final = 0o755
#: Added to a file that is already executable for its owner: read AND
#: execute, so `ExecStart=` and a verify hook's `argv[0]` can run as the
#: unit's own user.
FILE_OTHER_RX: Final = 0o005
#: Added to every other file: read only. Site-packages and data a process
#: opens, never a program a shell could run.
FILE_OTHER_R: Final = 0o004
OWNER_EXECUTE_BIT: Final = 0o100

#: The setting that says what a unit starts, and the one prefix of it that
#: does not. `ExecStartPre` is a check that runs and exits; `creche-noticeboard.
#: service` carries both, pointing at the same tree.
EXEC_START_KEY: Final = "ExecStart="

#: systemd's specifier for the home of the user a user unit runs as.
HOME_SPECIFIER_PREFIX: Final = "%h/"

#: systemd's prefix characters on an `ExecStart=` value, any number of them
#: in any order: `@` (argv[0] follows), `-` (a non-zero exit is not a
#: failure), `:` (no variable expansion), `+`, `!` and `!!` (privilege).
#: None is part of the path, and a reader that kept one would find every
#: prefixed unit outside every tree.
EXEC_PREFIXES: Final = "@-:+!"

#: The most of a unit file that is read. A unit is a few hundred bytes, and
#: root reads this one off disk as a file some other installer wrote.
MAX_UNIT_BYTES: Final = 64 * 1024

#: Rule 8: the most siblings one component may have. `attendance` has three.
#: More is a stage that fails, not a loop root runs over a directory the
#: operator can fill.
MAX_SIBLINGS: Final = 16

#: Rule 8's test 2: the only unit type a sibling can be.
SERVICE_SUFFIX: Final = ".service"

#: A template unit (`creche-trigger@.service`) has no instance of its own, so
#: it is refreshed and never restarted.
TEMPLATE_MARK: Final = "@."

#: Restarts a unit only when it runs. Rule 8 restarts a sibling with it.
TRY_RESTART: Final = "try-restart"

#: Opens a unit file root reads without following a link or blocking on a
#: FIFO the operator planted in their own unit directory.
_UNIT_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

GIT_TIMEOUT_S: Final = 300.0
BUILD_TIMEOUT_S: Final = 2400.0
SHORT_TIMEOUT_S: Final = 30.0

#: Step 9's restart. `systemctl restart` answers once the unit has stopped
#: and started again, and systemd bounds both (`TimeoutStopSec`, then
#: SIGKILL; `TimeoutStartSec`). So this only has to sit above the largest
#: sum a component unit allows: caregiver's 360 s drain plus a 90 s start.
#: `SHORT_TIMEOUT_S` cut attendance's 60 s stop on 2026-10-01 and restored a
#: healthy release. `test_a_restart_outlasts_every_units_own_stop_and_start`
#: holds every unit under it.
RESTART_TIMEOUT_S: Final = 600.0

#: §2.4 row 9: poll `is-active` and `NRestarts` for this long after a
#: restart. A unit that crash-loops climbs `NRestarts` inside the window.
SETTLE_WAIT_S: Final = 20.0
SETTLE_POLL_S: Final = 2.0

ACTIVE: Final = "active"

#: What `is-active` says of a unit nothing started, or one a person stopped.
INACTIVE: Final = "inactive"
RESTART: Final = "restart"
NRESTARTS_PROPERTY: Final = "NRestarts"

#: Contract 06 §4 rule 4's two caps on what a hook contributes.
VERIFY_STDOUT_MAX_BYTES: Final = 8 * 1024

#: Bounds a failing child's stderr, both in a verify hook's own `detail`
#: (contract 06 §4 rule 4) and in every other `_must` failure — a restart,
#: a unit refresh, a fetch. Without it `cannot restart creche-noticeboard.service
#: (exit 1)` drops the one line `systemctl` printed to explain itself.
STDERR_TAIL_LINES: Final = 40


class Swapped(StrEnum):
    """Which tree is live. A repair reads this off the switch journal."""

    NEW = "new"
    PREVIOUS = "previous"


class StepFailed(Exception):
    """A step that got as far as acting and then could not finish. Unlike a
    `Refusal`, the host may already have changed, so step 10 runs."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class VerifyOutcome:
    component: str
    ok: bool
    seconds: float
    detail: str


@dataclass(frozen=True)
class Paths:
    """One component's three trees, all checked for containment."""

    to: Path
    prev: Path
    new: Path


@dataclass(frozen=True)
class _Sibling:
    """Rule 8: one sibling unit, where it is installed and where the clone
    or the tree carries its file."""

    installed: Path
    carried: Path


def paths_of(manifest: ComponentManifest, roots: tuple[Path, ...]) -> Paths:
    """Contract 06 §8's `install`, with rule 2's containment check applied."""
    to = Path(manifest.install.to)
    prev = Path(manifest.install.prev)
    _require_contained(manifest.name, to, roots)
    if prev != Path(f"{to}{PREV_SUFFIX}"):
        detail = f"install.prev is not install.to{PREV_SUFFIX}: {prev.name}"
        raise Refusal(RefusalCode.MANIFEST, manifest.name, detail)

    return Paths(to=to, prev=prev, new=Path(f"{to}{NEW_SUFFIX}"))


def _require_contained(component: str, path: Path, roots: tuple[Path, ...]) -> None:
    """A direct child of a root. The root itself is refused: `swap_in`
    renames `to` away, and under a root that is every installed tree."""
    if not roots:
        return

    if path.parent in roots:
        return

    detail = f"install path is not directly under a configured install root: {path.name}"
    raise Refusal(RefusalCode.MANIFEST, component, detail)


class Installer:
    """Every filesystem and systemd action a switch performs.

    It holds the host and the install roots and nothing else, so a test
    drives the whole thing inside `tmp_path` with a recorded `run`.
    """

    def __init__(self, host: Host) -> None:
        self.host = host

    # -- stage ----------------------------------------------------------

    def fetch(self, repo: str, sha: str, into: Path) -> None:
        """Make the component's source at `sha` a root-owned tree.

        Root holds no git credential (contract 06 §3.4), so it reads a
        checkout another process keeps current, BY SHA. A SHA is content
        addressed, so where it travelled cannot change what it is — and the
        last command proves the tree is at that SHA and no other.

        The source is the CORPUS (`/srv/agents/code/<repo>`), the operator's own
        clone, which every sandbox sees read-only. It is not the platform
        root, which is a sandbox's rw mount: `executor/source.py` carries
        the whole argument. `require_trusted`
        runs before the first child, and every git call below carries
        root's own configuration on its command line, because
        `safe.directory` puts the repository's configuration back in play.
        """
        source = self.host.source_root / repo
        require_trusted(repo, source, self.host.source_owner_uid, self.host.source_owner_gid)
        ground = git_ground(source)
        env = git_env()
        self._must(
            [GIT, *ground, "-C", str(source), "cat-file", "-e", f"{sha}^{{commit}}"],
            As.ROOT,
            GIT_TIMEOUT_S,
            # The corpus is refreshed hourly, so a release filed minutes
            # after a merge names a SHA it may not hold yet. The one command
            # that fixes it goes in the refusal, because the ledger entry is
            # the only thing the operator reads afterwards.
            f"the corpus has no commit for {repo}'s resolved SHA; run {REFRESH_COMMAND}",
            env=env,
        )
        remove_tree(into)
        into.parent.mkdir(parents=True, exist_ok=True)
        # `<work root>/<request id>/` is made here with the
        # release unit's `UMask=0027`, so it is `root:root 0750`, and every
        # child of an `mcp-servers` build runs as `mcp-<name>` (`As.MCP`)
        # in a tree underneath it: `uv pip install` reads
        # `<request id>/mcp/<name>/install.lock`, `uv sync --frozen` runs
        # with its `cwd` in this very checkout. None of those users is in
        # group root, so all of them fail at step 8 on the DIRECTORY,
        # whatever modes `mcpbuild` opens below it.
        normalize_dir(into.parent)
        self._must(
            [GIT, *ground, "clone", "--quiet", "--no-checkout", "--local", str(source), str(into)],
            As.ROOT,
            GIT_TIMEOUT_S,
            f"cannot clone {repo}",
            env=env,
        )
        # The clone is root's own tree from here on, so its ground names the
        # DESTINATION. It is not dropped: root's own gitconfig must not
        # decide what a checkout does either, and a `.gitattributes` in the
        # fetched tree can name a filter or a textconv driver.
        into_ground = git_ground(into)
        self._must(
            [GIT, *into_ground, "-C", str(into), "checkout", "--quiet", "--detach", sha],
            As.ROOT,
            GIT_TIMEOUT_S,
            f"cannot check out the resolved SHA of {repo}",
            env=env,
        )
        head = self._must(
            [GIT, *into_ground, "-C", str(into), "rev-parse", "HEAD"],
            As.ROOT,
            SHORT_TIMEOUT_S,
            f"cannot read {repo}'s HEAD",
            env=env,
        )
        if head.strip() != sha:
            raise StepFailed(f"{repo}'s work tree is not at the resolved SHA")

    def check_unit_binds(self, manifest: ComponentManifest, source: Path) -> None:
        """Contract 06 §1 rule 8: a release must be able to change what runs.

        When a unit execs `/opt/creche/.venv/bin/chaperone` while
        `chaperone/component.yaml` installs `/opt/components/chaperone`, nothing in
        steps 8 to 10 notices: the tree builds, the swap lands,
        the unit restarts, `chaperone-verify` passes out of the NEW tree, and the
        running PEP is the old code of `/opt`. The ledger says `succeeded`
        and §2.5's tap bound to a commit that never went into service.

        Three narrowings, each with its reason.

        1. **`unit: null` is not checked.** Nothing restarts, so there is no
           unit to disagree with the tree (`handover`, `playpen`).
        2. **Only `kind: venv` and `kind: binary`** (`PROGRAM_KINDS`).
           "The tree holds the program the unit starts" is the property of
           a venv and of a tree of compiled programs. A compose project's
           unit starts docker and is bound to its tree by
           `WorkingDirectory`; an image has no unit at all.
        3. **A unit nothing installed is not checked.** Step 9 already
           lists one under `manual` and never restarts it, and refusing
           here would refuse the first release of a component whose unit
           the installer has not put in yet.

        The unit that is READ is the one that will be in force after step 9,
        which refreshes the file in place from the fetched tree: otherwise
        the release that CORRECTS a unit would be the one release refused.
        A wrapper fails this rule, and that is the intended answer — root
        cannot follow an exec chain without running it. The two wrappers on
        this host, `/usr/local/sbin/creche-handover` and
        `-intake`, belong to `handover`, which carries `unit: null`
        (contract 06 §1.1 rule 5), so no component with a unit goes through
        one today.
        """
        if manifest.unit is None or manifest.kind not in PROGRAM_KINDS:
            return

        found = self._effective_unit(manifest, source)
        if found is None:
            return

        where, text = found
        to = Path(manifest.install.to)
        programs = exec_start_programs(text)
        if not self._is_system_unit(manifest):
            programs = tuple(_at_home(one) for one in programs)

        outside = [one for one in programs if not _starts_inside(to, one)]
        if programs and not outside:
            return

        # Invariant 19's fail-closed end for the empty case: a unit root
        # cannot read a program out of is a unit root cannot say starts the
        # tree.
        starts = safe_token(outside[0]) if outside else "nothing this reader could find"
        detail = (
            f"{manifest.unit} starts {starts}, which is not inside {manifest.install.to}"
            f" ({where.name} read from {where.parent}): this release would change"
            " nothing that runs"
        )

        raise Refusal(RefusalCode.UNIT, manifest.name, detail)

    def _effective_unit(self, manifest: ComponentManifest, source: Path) -> tuple[Path, str] | None:
        """The unit file that will be in force after step 9, and its text.

        `refresh_unit`'s own choice, made one step earlier: the staged file
        wins where one is installed, the clone carries it (`stage_unit`'s
        test), and the installed one is not a symlink — which are exactly
        the three conditions under which step 9 copies it over.
        """
        installed = self._installed_unit(manifest)
        if installed is None:
            return None

        carried = _carried_unit(manifest, source)
        chosen = carried if carried is not None and not installed.is_symlink() else installed
        try:
            data = chosen.read_bytes()[:MAX_UNIT_BYTES]
        except OSError:
            raise StepFailed(f"{manifest.name}: cannot read {manifest.unit}") from None

        return chosen, data.decode("utf-8", errors="replace")

    def build(self, manifest: ComponentManifest, source: Path, paths: Paths) -> None:
        """Run `build` into `<install.to>.new`, then prove the hook is there."""
        remove_tree(paths.new)
        paths.new.parent.mkdir(parents=True, exist_ok=True)
        self._make_relocatable(manifest, source, paths)
        for argv in manifest.build:
            self._must(
                list(argv),
                As.ROOT,
                BUILD_TIMEOUT_S,
                f"{manifest.name}: build failed",
                cwd=source,
                env={_output_env(manifest.kind): str(paths.new)},
            )

        self._check_staged_hook(manifest, paths)
        self._check_self_contained(manifest, paths)

    def _check_self_contained(self, manifest: ComponentManifest, paths: Paths) -> None:
        """Contract 06 §8.2, applied after the build and before the swap.

        Only `kind: venv`: a compose project and an image tree have no
        site-packages, and the walk would refuse both for the one reason
        that cannot apply to them.

        A `Refusal` and not a `StepFailed`, because nothing on the host has
        moved: the old tree is still in service and the ledger carries a
        code that says which fault this was.
        """
        if manifest.kind is not Kind.VENV:
            return

        check_tree(manifest.name, paths.new)

    def _make_relocatable(self, manifest: ComponentManifest, source: Path, paths: Paths) -> None:
        """`RELOCATABLE_ARGV`'s reason, applied to one component.

        Only `kind: venv`. A compose project and an image tree have no
        console script to break, and running `uv venv` at either would
        create a directory the build then has to work around.
        """
        if manifest.kind is not Kind.VENV:
            return

        self._must(
            [*RELOCATABLE_ARGV, str(paths.new)],
            As.ROOT,
            BUILD_TIMEOUT_S,
            f"{manifest.name}: the staged environment could not be created",
            cwd=source,
        )

    def _check_staged_hook(self, manifest: ComponentManifest, paths: Paths) -> None:
        """Rule 3: the hook the switch will run must exist under `.new` and
        must not point out of it."""
        if not paths.new.is_dir():
            raise StepFailed(f"{manifest.name}: build wrote no {paths.new.name}")

        staged = _relocate(manifest.verify.command[0], paths.to, paths.new)
        if staged is None:
            raise StepFailed(f"{manifest.name}: the verify hook is outside install.to")

        real = staged.resolve()
        if not real.is_file() or paths.new.resolve() not in real.parents:
            raise StepFailed(f"{manifest.name}: the staged verify hook is missing or escapes")

    def stage_unit(self, manifest: ComponentManifest, source: Path, new: Path) -> None:
        """Rule 7: the clone's unit file, copied into the staged tree.

        Called before `normalize_modes`, so the copy is readable by the
        unit's owner whatever the release unit's umask. Nothing is staged
        when the clone carries no file for the unit, and `refresh_unit`
        then says so under `manual`.

        Rule 8: every sibling's file is staged too, so later steps read
        siblings out of the tree and never out of the clone.
        """
        carried = _carried_unit(manifest, source)
        if carried is not None:
            _stage_file(manifest.name, carried, new / UNIT_DIR_IN_TREE / str(manifest.unit))

        for sibling in self._siblings(manifest, source / UNIT_DIR_IN_REPO):
            target = new / UNIT_DIR_IN_TREE / sibling.installed.name
            _stage_file(manifest.name, sibling.carried, target)

    def normalize_modes(self, root: Path) -> None:
        """The module function below, as a step of the switch. `steps.py`
        calls it here, and `mcpbuild` calls the function directly."""
        normalize_modes(root)

    def compose_recreates(self, manifest: ComponentManifest, source: Path) -> tuple[str, ...]:
        """§2.8: which of the three watched services a compose diff touches.

        `build` for a `kind: compose` component IS the dry run (contract 06
        §8), so step 8 has already run it. This runs it once more and reads
        the service names out of what it printed, because a dry run prints
        what it would do and changes nothing.

        The read is a substring scan over `quiet.WATCHED_SERVICES` and not
        a parse of docker's output format. A scan cannot be wrong about a
        format that changes: it can only over-report, and over-reporting
        costs a wait that was not needed. Under-reporting would cost every
        in-flight model call on the host.
        """
        printed = ""
        for argv in manifest.build:
            printed += self._run(list(argv), As.ROOT, BUILD_TIMEOUT_S, cwd=source).stdout

        return tuple(name for name in WATCHED_SERVICES if name in printed)

    # -- switch ---------------------------------------------------------

    def swap_in(self, paths: Paths) -> None:
        """`<to>` to `<prev>`, then `<new>` to `<to>`. Two renames inside one
        filesystem, so each is atomic and there is no window with no tree."""
        remove_tree(paths.prev)
        if paths.to.exists():
            os.rename(paths.to, paths.prev)

        os.rename(paths.new, paths.to)

    def swap_back(self, paths: Paths) -> None:
        """Put `<prev>` back. The failed tree goes to `<new>`, and the run
        that staged it removes it before it ends (`steps.remove_staged`)."""
        if not paths.prev.is_dir():
            raise StepFailed("no previous artifact to restore")

        remove_tree(paths.new)
        if paths.to.exists():
            os.rename(paths.to, paths.new)

        os.rename(paths.prev, paths.to)

    def refresh_unit(self, manifest: ComponentManifest, tree: Path) -> str | None:
        """Rule 4. Returns what the caller must list under `manual`, or None.

        `tree` is the LIVE tree, after the swap (rule 7): the unit's owner
        can read it, and the clone it came from they cannot. A unit that is
        not installed is NOT installed here. Installing one stays the
        installer's job.
        """
        if manifest.unit is None:
            return None

        staged = tree / UNIT_DIR_IN_TREE / manifest.unit
        installed = self._installed_unit(manifest)
        if installed is None:
            return f"not installed: {manifest.unit} (the installer owns it)"

        if not staged.is_file():
            return f"no unit file in the source tree: {manifest.unit}"

        if self._replaced_unit(manifest, tree) is None:
            return None

        identity = As.ROOT if self._is_system_unit(manifest) else As.OPERATOR
        self._must(
            [INSTALL, "-m", UNIT_FILE_MODE, str(staged), str(installed)],
            identity,
            SHORT_TIMEOUT_S,
            f"cannot refresh {manifest.unit}",
        )

        return None

    def keep_unit(self, manifest: ComponentManifest, tree: Path) -> Path | None:
        """Rule 6's first half: the copy's path, or None when `refresh_unit`
        will replace nothing. `tree` is the STAGED tree, before the swap:
        the same file `refresh_unit` reads after it.

        Beside the unit, and not in the release's work tree, for three
        reasons. A user unit goes back by `install` run as the operator, who cannot
        read what root writes there under the release unit's `UMask=0027`.
        Root copying the operator's file by path into one the operator can read would make
        root their deputy (`handover/AGENTS.md` rule 5a). And the work tree's
        parent, `/srv/agents/work`, is the operator's (`bin/bootstrap-root.sh`), so
        a copy under it could be swapped before root installs it as a
        system unit. In the unit's own directory, only who could change the
        unit can change the copy.
        """
        installed = self._replaced_unit(manifest, tree)
        if installed is None:
            return None

        return self._keep(installed)

    def _keep(self, installed: Path) -> Path:
        """One unit file copied beside itself, as its directory's owner."""
        kept = installed.with_name(installed.name + UNIT_KEPT_SUFFIX)
        self._must(
            [INSTALL, "-m", UNIT_FILE_MODE, str(installed), str(kept)],
            self._owner_of(kept),
            SHORT_TIMEOUT_S,
            f"cannot keep {installed.name}",
        )

        return kept

    def keep_siblings(self, manifest: ComponentManifest, tree: Path) -> tuple[Path, ...]:
        """Rule 8, with rule 6's first half: every sibling the refresh will
        replace, kept beside itself. `tree` is the STAGED tree, before the
        switch note. A keep that fails raises before the swap, so nothing
        has moved."""
        kept: list[Path] = []
        for sibling in self._siblings(manifest, tree / UNIT_DIR_IN_TREE):
            if _read_unit(sibling.installed) == _read_unit(sibling.carried):
                continue

            kept.append(self._keep(sibling.installed))

        return tuple(kept)

    def refresh_siblings(
        self, manifest: ComponentManifest, tree: Path, kept: tuple[Path, ...]
    ) -> tuple[Path, ...]:
        """Rule 8, with rule 4: each sibling `keep_siblings` kept, installed
        from the LIVE tree as its directory's owner. Answers the unit files
        it wrote.

        The set is recomputed here, and only a sibling that is still one
        AND was kept is written: nothing is installed without a copy to
        put back.
        """
        names = {_unit_of(one).name for one in kept}
        refreshed: list[Path] = []
        for sibling in self._siblings(manifest, tree / UNIT_DIR_IN_TREE):
            name = sibling.installed.name
            if name not in names:
                continue

            self._must(
                [INSTALL, "-m", UNIT_FILE_MODE, str(sibling.carried), str(sibling.installed)],
                self._owner_of(sibling.installed),
                SHORT_TIMEOUT_S,
                f"cannot refresh {name}",
            )
            refreshed.append(sibling.installed)

        return tuple(refreshed)

    def active_units(self, units: tuple[Path, ...]) -> tuple[Path, ...]:
        """The units of `units` that run now and are not templates. Asked
        BEFORE the main restart: a sibling that was stopped stays stopped."""
        active: list[Path] = []
        for unit in units:
            if TEMPLATE_MARK in unit.name:
                continue

            scope, identity = self._scope_of(unit)
            said = self._run([*scope, "is-active", unit.name], identity, SHORT_TIMEOUT_S)
            if said.stdout.strip() == ACTIVE:
                active.append(unit)

        return tuple(active)

    def try_restart(self, units: tuple[Path, ...]) -> None:
        """Rule 8, on the switch: `systemctl try-restart` for each unit that
        is not a template, AFTER the main restart and its `daemon-reload`.
        A unit that does not settle fails the step, and the release goes
        down the restore path."""
        for unit in units:
            if TEMPLATE_MARK in unit.name:
                continue

            scope, identity = self._scope_of(unit)
            self._must(
                [*scope, TRY_RESTART, unit.name],
                identity,
                RESTART_TIMEOUT_S,
                f"cannot restart {unit.name}",
            )
            self._settle(unit.name, scope, identity)

    def restart_units(self, units: tuple[Path, ...]) -> None:
        """Rule 8, on a restore: `systemctl restart` for each unit that is
        not a template. For siblings the caller KNOWS ran before the switch.
        `try-restart` would leave one down that the new tree made fail:
        it does nothing for a unit in `failed`."""
        for unit in units:
            if TEMPLATE_MARK in unit.name:
                continue

            scope, identity = self._scope_of(unit)
            self._must(
                [*scope, RESTART, unit.name],
                identity,
                RESTART_TIMEOUT_S,
                f"cannot restart {unit.name}",
            )

    def revive(self, units: tuple[Path, ...]) -> None:
        """Rule 8, on a restore with no memory of what ran: restart each
        unit that is not `inactive`. `failed` and `activating` both mean it
        was meant to run. One a person stopped stays stopped."""
        waiting: list[Path] = []
        for unit in units:
            if TEMPLATE_MARK in unit.name:
                continue

            scope, identity = self._scope_of(unit)
            said = self._run([*scope, "is-active", unit.name], identity, SHORT_TIMEOUT_S)
            if said.stdout.strip() != INACTIVE:
                waiting.append(unit)

        self.restart_units(tuple(waiting))

    def sibling_units(self, manifest: ComponentManifest, tree: Path) -> tuple[Path, ...]:
        """Rule 8: the installed unit file of every sibling `tree` carries,
        changed by this release or not."""
        found = self._siblings(manifest, tree / UNIT_DIR_IN_TREE)

        return tuple(one.installed for one in found)

    def restore_unit(self, kept: Path) -> Path:
        """Rule 6's second half: the kept copy back over the unit it came
        from. Answers the unit file it wrote; the caller's restart does the
        `daemon-reload`.

        `kept` may come out of a switch note, so who writes is read off its
        DIRECTORY and never off the note: root writes only into the system
        unit directory, and anywhere else the write is the operator's.
        """
        installed = _unit_of(kept)
        self._must(
            [INSTALL, "-m", UNIT_FILE_MODE, str(kept), str(installed)],
            self._owner_of(kept),
            SHORT_TIMEOUT_S,
            f"cannot put back {installed.name}",
        )

        return installed

    def unit_by_hand(self, kept: Path) -> str:
        """What `manual` says when `restore_unit` could not: the same
        `install`, then the reload and the restart the restore did not run.
        The previous tree is already back, so this is all a person types."""
        installed = _unit_of(kept)
        who = self._owner_of(kept)
        systemctl = "systemctl" if who is As.ROOT else "systemctl --user"
        # A person types this, so it names the account they log in as.
        account = operator_user() if who is As.OPERATOR else str(who)

        return (
            f"restore: {installed.name} is still this release's unit file; as {account}: "
            f"install -m {UNIT_FILE_MODE} {kept} {installed} && {systemctl} daemon-reload"
            f" && {systemctl} restart {installed.name}"
        )

    def restart(self, manifest: ComponentManifest) -> None:
        """`daemon-reload`, restart, then poll until the unit settles."""
        self.restart_unit(manifest.unit)

    def restart_unit(self, unit: str | None) -> None:
        """The same, from a unit NAME alone.

        A repair has no manifest — the run that resolved one crashed — and
        it still has to restart what it put back. The unit name comes out
        of the switch note root wrote before the swap.
        """
        if unit is None or self._installed(unit) is None:
            return

        system = self._is_system(unit)
        identity = As.ROOT if system else As.OPERATOR
        scope = [SYSTEMCTL] if system else [SYSTEMCTL, "--user"]
        self._must([*scope, "daemon-reload"], identity, SHORT_TIMEOUT_S, "daemon-reload")
        self._must(
            [*scope, "restart", unit],
            identity,
            RESTART_TIMEOUT_S,
            f"cannot restart {unit}",
        )
        self._settle(unit, scope, identity)

    def _settle(self, unit: str, scope: list[str], identity: As) -> None:
        """§2.4 row 9: `is-active` and `NRestarts` for 20 seconds. A unit
        that comes up and then crash-loops climbs `NRestarts` inside it."""
        baseline = self._restarts(scope, identity, unit)
        waited = 0.0
        while True:
            active = self._run([*scope, "is-active", unit], identity, SHORT_TIMEOUT_S)
            if active.stdout.strip() != ACTIVE:
                raise StepFailed(f"{unit} is not active after a restart")

            if self._restarts(scope, identity, unit) != baseline:
                raise StepFailed(f"{unit} restarted again inside {SETTLE_WAIT_S:.0f}s")

            if waited >= SETTLE_WAIT_S:
                return

            self.host.sleep(SETTLE_POLL_S)
            waited += SETTLE_POLL_S

    def _restarts(self, scope: list[str], identity: As, unit: str) -> str:
        argv = [*scope, "show", "-p", NRESTARTS_PROPERTY, "--value", unit]

        return self._run(argv, identity, SHORT_TIMEOUT_S).stdout.strip()

    # -- verify ---------------------------------------------------------

    def verify(self, manifest: ComponentManifest) -> VerifyOutcome:
        """Contract 06 §4. Exit 0 passes, anything else fails, a timeout is
        a failure, and the hook runs as its own `user`.

        `VerifyUser` and `As` are two enums over the same two words, so the
        mapping is written out. Comparing a `VerifyUser` member with `is`
        against a plain string is never true, and the hook then ran as the operator
        whatever the manifest said.
        """
        hook = VerifyHook(
            command=tuple(manifest.verify.command),
            user=str(manifest.verify.user),
            timeout_s=manifest.verify.timeout_s,
        )

        return self.run_hook(manifest.name, hook)

    def run_hook(self, component: str, hook: VerifyHook) -> VerifyOutcome:
        """The same, from the three fields alone, so a repair can run the
        hook the switch note recorded.

        stdout is captured pass OR fail, because a hook's report is what a
        reader needs to learn WHY, and `noticeboard-verify --json` writes its whole
        report to stdout and nothing to stderr (contract 06 §4 rule 4).
        """
        identity = As.ROOT if hook.user == str(VerifyUser.ROOT) else As.OPERATOR
        started = self.host.clock()
        result = self._run(list(hook.command), identity, float(hook.timeout_s))
        seconds = round(self.host.clock() - started, 1)
        stdout = result.stdout[:VERIFY_STDOUT_MAX_BYTES]
        if result.code == 0:
            return VerifyOutcome(component, True, seconds, stdout)

        detail = f"exit {result.code}: {stdout}"
        tail = result.stderr.splitlines()[-STDERR_TAIL_LINES:]
        if tail:
            detail += f" | stderr: {' | '.join(tail)}"

        return VerifyOutcome(component, False, seconds, detail)

    # -- plumbing -------------------------------------------------------

    def _installed(self, unit: str) -> Path | None:
        for directory in (self.host.system_unit_dir, self.host.user_unit_dir):
            candidate = directory / unit
            if candidate.is_file():
                return candidate

        return None

    def _installed_unit(self, manifest: ComponentManifest) -> Path | None:
        return self._installed(manifest.unit) if manifest.unit else None

    def _replaced_unit(self, manifest: ComponentManifest, tree: Path) -> Path | None:
        """The installed unit file `refresh_unit` overwrites, or None.

        Rule 4's four conditions: one is installed, the release's tree
        carries one, the installed one is not a symlink, and the two differ.
        `keep_unit` asks the same question before the switch note, so the
        switch keeps exactly the file it then replaces.
        """
        if manifest.unit is None:
            return None

        staged = tree / UNIT_DIR_IN_TREE / manifest.unit
        installed = self._installed_unit(manifest)
        if installed is None or installed.is_symlink() or not staged.is_file():
            return None

        if installed.read_bytes() == staged.read_bytes():
            return None

        return installed

    def _owner_of(self, path: Path) -> As:
        """Who writes a file in a unit directory: root in the system one,
        the operator anywhere else."""
        return As.ROOT if path.parent == self.host.system_unit_dir else As.OPERATOR

    def _scope_of(self, unit: Path) -> tuple[list[str], As]:
        """`systemctl` and its identity for an installed unit file, read
        off its directory as `_owner_of` reads it."""
        identity = self._owner_of(unit)
        scope = [SYSTEMCTL] if identity is As.ROOT else [SYSTEMCTL, "--user"]

        return scope, identity

    def _siblings(self, manifest: ComponentManifest, carried_dir: Path) -> tuple[_Sibling, ...]:
        """Rule 8's four tests, over `carried_dir`: the `systemd/` of a
        clone or of a staged or live tree. Sorted by name, and at most
        `MAX_SIBLINGS`.

        The NAMES come from the release, which is root's and holds a few
        files. The unit directory is never listed: a user one is the
        operator's, and a listing would walk whatever they put there. Root
        opens only `<unit directory>/<a name the release carries>`.
        """
        if manifest.kind not in PROGRAM_KINDS:
            return ()

        installed = self._installed_unit(manifest)
        if installed is None:
            return ()

        found: list[_Sibling] = []
        for name in _service_names(carried_dir):
            if name == manifest.unit:
                continue

            carried = _inside(carried_dir.parent, carried_dir / name)
            if carried is None or not self._starts_tree(manifest, carried):
                continue

            path = installed.parent / name
            if self._starts_tree(manifest, path):
                found.append(_Sibling(installed=path, carried=carried))

        if len(found) > MAX_SIBLINGS:
            detail = f"{len(found)} sibling units of {manifest.unit}, more than {MAX_SIBLINGS}"
            raise StepFailed(f"{manifest.name}: {detail}")

        return tuple(found)

    def _starts_tree(self, manifest: ComponentManifest, unit: Path) -> bool:
        """`check_unit_binds`' test for one unit file: every `ExecStart=`
        program inside `install.to`, `%h/` resolved for a user unit. A file
        with none, or one root cannot read, does not start the tree."""
        data = _read_unit(unit)
        if data is None:
            return False

        programs = exec_start_programs(data.decode("utf-8", errors="replace"))
        if not self._is_system_unit(manifest):
            programs = tuple(_at_home(one) for one in programs)

        to = Path(manifest.install.to)

        return bool(programs) and all(_starts_inside(to, one) for one in programs)

    def _is_system(self, unit: str) -> bool:
        return (self.host.system_unit_dir / unit).is_file()

    def _is_system_unit(self, manifest: ComponentManifest) -> bool:
        return bool(manifest.unit) and self._is_system(manifest.unit or "")

    def _run(
        self,
        argv: list[str],
        identity: As,
        timeout: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Result:
        command = Command(
            argv=tuple(argv),
            identity=identity,
            timeout_s=timeout,
            cwd=cwd,
            env=tuple(sorted((env or {}).items())),
        )
        try:
            return self.host.run(command)
        except OSError as exc:
            raise StepFailed(f"{argv[0]}: {type(exc).__name__}") from None

    def _must(
        self,
        argv: list[str],
        identity: As,
        timeout: float,
        what: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        result = self._run(argv, identity, timeout, cwd, env)
        if result.code != 0:
            raise StepFailed(f"{what} (exit {result.code}){_stderr_suffix(result.stderr)}")

        return result.stdout


def _stderr_suffix(stderr: str) -> str:
    """What `_must` adds to a failure so the ledger's `steps` row
    carries what the child said, not only its exit code. Empty when the
    child wrote nothing, which is the common case for a signal or a
    permission error `subprocess` itself turns into `Result`."""
    tail = stderr.splitlines()[-STDERR_TAIL_LINES:]
    if not tail:
        return ""

    return f": {' | '.join(tail)}"


def _output_env(kind: Kind) -> str:
    """The variable through which a build of this kind learns where
    `<install.to>.new` is. Every kind but `binary` keeps the one it had."""
    return BINARY_ENV_NAME if kind is Kind.BINARY else VENV_ENV_NAME


def restorable(manifest: ComponentManifest) -> bool:
    """Contract 06 §5.2. A component that ran a forward-only migration says
    so in its own file, and the executor believes the file: blind reversal
    after a migration is more dangerous than stopping."""
    return manifest.restore.mode is RestoreMode.AUTOMATIC


def exec_start_programs(text: str) -> tuple[str, ...]:
    """Every program the unit file `text` starts, in the order it lists them.

    A parse of the FILE, and not of `systemctl show -p ExecStart`. Three of
    the four units a release restarts are USER units of the operator's, installed
    under `~operator/.config/systemd/user` by `bin/rework-cutover.sh`, and
    their properties come off the operator's own bus: root reaches it only through
    `runuser` with an `XDG_RUNTIME_DIR` that exists while the operator is logged
    in. The file is there either way, and `refresh_unit` already reads the
    same two directories.

    systemd's own rules, each of which is a way to read this wrongly:
    a line continues while it ends in a backslash, an EMPTY assignment
    resets the list, and the value may carry any number of the prefix
    characters in `EXEC_PREFIXES` before the path.
    """
    found: list[str] = []
    for line in _logical_lines(text):
        stripped = line.strip()
        if not stripped.startswith(EXEC_START_KEY):
            continue

        value = stripped[len(EXEC_START_KEY) :].strip()
        if not value:
            found.clear()
            continue

        program = _program_of(value)
        if program:
            found.append(program)

    return tuple(found)


def _logical_lines(text: str) -> list[str]:
    """Unit-file lines with every backslash continuation joined onto one.
    `creche-caregiver.service` writes its arguments over four."""
    joined: list[str] = []
    carried = ""
    for line in text.splitlines():
        carried += line.removesuffix("\\") if line.rstrip().endswith("\\") else line
        if line.rstrip().endswith("\\"):
            continue

        joined.append(carried)
        carried = ""

    if carried:
        joined.append(carried)

    return joined


def _program_of(value: str) -> str:
    """`argv[0]` of one `ExecStart=` value, unprefixed and unquoted."""
    word = value.lstrip(EXEC_PREFIXES)
    try:
        words = shlex.split(word)
    except ValueError:
        words = word.split()

    return words[0] if words else ""


def _at_home(program: str) -> str:
    """`%h/x` as `<the operator's home>/x`, which is what systemd makes of
    it in a user unit. A unit file names no account, so it reaches its tree
    through `%h`, and the manifest's `install.to` is that same home."""
    if not program.startswith(HOME_SPECIFIER_PREFIX):
        return program

    return f"{operator_home()}/{program[len(HOME_SPECIFIER_PREFIX) :]}"


def _starts_inside(root: Path, program: str) -> bool:
    """Whether `program` is the tree `root` or a path within it.

    Textual, after `normpath`, and deliberately without `resolve`: the
    manifest says which directory the release installs, and following a
    symlink on the live host would answer about a different question. A
    relative `ExecStart` — systemd resolves one on its own `PATH` — is
    never inside a tree this executor installs.
    """
    candidate = Path(os.path.normpath(program))
    if not candidate.is_absolute():
        return False

    return candidate == root or root in candidate.parents


def _carried_unit(manifest: ComponentManifest, source: Path) -> Path | None:
    """The clone's file for the manifest's unit, or None: no unit named, no
    file there, or a path that resolves outside the clone.

    Root copies this into a tree everyone can read (rule 7), so a symlink
    out of the clone — `systemd/<unit>` itself, or `systemd/` — must never
    become a unit file: it would publish whatever root alone may read.
    """
    if manifest.unit is None:
        return None

    carried = source / UNIT_DIR_IN_REPO / manifest.unit
    real = carried.resolve()
    if not real.is_file() or source.resolve() not in real.parents:
        return None

    return carried


def _stage_file(component: str, carried: Path, target: Path) -> None:
    """Rule 7's copy of one unit file into the staged tree."""
    try:
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(carried, target)
    except OSError:
        raise StepFailed(f"{component}: cannot stage {target.name}") from None


def _service_names(directory: Path) -> list[str]:
    """Every regular `*.service` a release's `systemd/` holds, by name. A
    symlink is skipped, never followed. `directory` is a clone's or a
    tree's: root's own, and as small as the repository makes it."""
    try:
        with os.scandir(directory) as entries:
            names = [
                one.name
                for one in entries
                if one.name.endswith(SERVICE_SUFFIX) and one.is_file(follow_symlinks=False)
            ]
    except OSError:
        return []

    return sorted(names)


def _inside(root: Path, path: Path) -> Path | None:
    """`path` when it is a file that resolves inside `root`, else None.
    `_carried_unit`'s test: no symlink out of the clone or the tree."""
    real = path.resolve()
    if not real.is_file() or root.resolve() not in real.parents:
        return None

    return path


def _read_unit(path: Path) -> bytes | None:
    """A unit file's bytes, or None: a symlink, not a regular file, longer
    than `MAX_UNIT_BYTES`, or unreadable.

    `O_NOFOLLOW` and `O_NONBLOCK`, because a file in the operator's unit
    directory may change into a link or a FIFO between the listing and
    this read, and root must neither follow one nor hang on one.
    """
    try:
        fd = os.open(path, _UNIT_READ_FLAGS)
    except OSError:
        return None

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None

        # To EOF, never past the cap plus one: one `read` may return less.
        data = b""
        while len(data) <= MAX_UNIT_BYTES and (
            chunk := os.read(fd, MAX_UNIT_BYTES + 1 - len(data))
        ):
            data += chunk
    except OSError:
        return None
    finally:
        os.close(fd)

    return data if len(data) <= MAX_UNIT_BYTES else None


def _unit_of(kept: Path) -> Path:
    """`/etc/systemd/system/creche-chaperone.service` for its kept copy. A name
    with nothing before the suffix answers itself, so the `install` fails
    and says so rather than this raising inside a repair."""
    name = kept.name.removesuffix(UNIT_KEPT_SUFFIX)

    return kept.parent / name if name else kept


def _relocate(command: str, old_root: Path, new_root: Path) -> Path | None:
    """`/opt/components/chaperone/bin/chaperone-verify` under `<to>.new` instead."""
    candidate = Path(command)
    if candidate != old_root and old_root not in candidate.parents:
        return None

    return new_root / candidate.relative_to(old_root)


def normalize_modes(root: Path) -> None:
    """Called once `root` holds everything a stage writes (the
    build's own output, the version stamp, the manifest stamp), so every
    mode is fixed in one pass and nothing written after is missed.

    Every directory becomes `0755`. Every file keeps its own bits and gains
    read for OTHER, plus execute for OTHER when it was already executable
    for its owner — that second half is what lets `ExecStart=` and a verify
    hook's `argv[0]` run as the unit's own user; read alone would leave
    every console script unusable. Nothing gains a write bit: root built
    this tree and stays the only writer.

    Symlinks are skipped entirely, never followed. `chmod` follows a
    symlink to its target, and a relocatable venv's tree can hold one that
    points outside `root` — a system interpreter, a shared library. Leaving
    a mode we did not mean to touch alone is safer than changing one on a
    file outside the tree being staged.

    Public, and not a method, because the fault is not the switch's:
    `mcpbuild` hands trees to unprivileged users too, and a
    second expression of this rule is how two trees end up with two
    answers.
    """
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        normalize_dir(current)
        for filename in filenames:
            _normalize_file(current / filename)


def normalize_dir(path: Path) -> None:
    """ONE directory of that pass, for a caller that must open a directory
    without walking what is under it.

    `/opt/mcp` is the case it exists for: every `mcp-<name>` must traverse
    it to reach its own tree, and it holds every OTHER server's live tree,
    which this release is not staging and must not re-mode.
    """
    if path.is_symlink():
        return

    os.chmod(path, DIR_MODE)


def _normalize_file(path: Path) -> None:
    if path.is_symlink():
        return

    mode = stat.S_IMODE(path.stat().st_mode)
    grant = FILE_OTHER_RX if mode & OWNER_EXECUTE_BIT else FILE_OTHER_R
    os.chmod(path, mode | grant)


def remove_tree(path: Path) -> None:
    """Remove a tree root owns. A symlink is unlinked, never followed.

    Public because `mcpbuild` stages one tree per MCP server beside these
    and must remove a stale `.new` the same way."""
    if path.is_symlink() or path.is_file():
        path.unlink()

        return

    if path.is_dir():
        shutil.rmtree(path)
