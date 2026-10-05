"""The systemd proof fails for each result that breaks the restart rule.

`bin/systemd-proof.sh` proves on a Linux runner that a unit with
`RestartPreventExitStatus=78` stays stopped after exit status 78
(`rust/AGENTS.md`, "The config of a process"). Only that run is the proof.
This file holds the script itself: a proof that cannot fail proves nothing.
Six things could go wrong without one red line, and each gets a check here:

1. **A case that passes on a wrong result.** Case 1 fails for a unit that is
   not `failed`, for another exit status and for one restart. Case 2 and
   case 3 fail for a unit that systemd does not start again. A value of
   `NRestarts` that is no count fails, and does not read as zero.
2. **A unit that stays on the machine.** The script removes its three units
   at its end, also after a failed case and after a start that failed. It
   fails when systemd still holds one.
3. **A machine that cannot run the proof, and passes.** Without systemd as
   process 1, without `sudo` or without one of the tools, the script fails
   before it starts a unit.
4. **A unit file that the tool refuses, and that passes.** The one line that
   passes is the line for a program that the machine does not hold. Each
   other line refuses the unit, and so does a failure with no line.
5. **A change that needs the proof, and skips it.** `--unchanged` says no
   for a change that git cannot read, and a wrong call is no answer. The
   table of the changes is in `bin/tests/test_gate_workflow.py`.
6. **A proof of another line than the one the Rust code names.** The status
   and the unit line of the script equal `EX_CONFIG` and `NO_RESTART_LINE`
   of `rust/crates/creche-contracts/src/config.rs`.

Each test runs the real script in a throwaway tree. `sudo`, `ps`, `sleep`,
`systemctl`, `systemd-run` and `systemd-analyze` are fakes that write their
argv to a file. PATH holds only those fakes and links to the few tools that
the script and the fakes call, so no test reaches the systemd of the machine.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: The file under test, copied into the throwaway tree.
PROOF = "bin/systemd-proof.sh"

#: The home of the two Rust constants that the script copies.
RUST_CONFIG = REPO / "rust" / "crates" / "creche-contracts" / "src" / "config.rs"
RUST_STATUS = re.compile(r"^pub const EX_CONFIG: u8 = (\d+);$", re.MULTILINE)
RUST_LINE = re.compile(r'^pub const NO_RESTART_LINE: &str = "([^"]+)";$', re.MULTILINE)

#: Every program that the script and the fakes call, but the fakes.
TOOLS = ("bash", "dirname", "git", "sed", "rm", "cat", "true")

#: The three test units, one for each case.
MAIN_REFUSES = "creche-proof-main-78.service"
MAIN_FAILS = "creche-proof-main-1.service"
CHECK_REFUSES = "creche-proof-pre-78.service"
TEST_UNITS = (MAIN_REFUSES, MAIN_FAILS, CHECK_REFUSES)

#: What each test unit holds, as options of `systemd-run`.
RESTART_RULE = [
    "--property=Restart=always",
    "--property=RestartSec=1",
    "--property=StartLimitIntervalSec=0",
    "--property=RestartPreventExitStatus=78",
]

#: What follows the restart rule in each call of `systemd-run`: the main
#: process, and for case 3 the process that runs before it.
STARTS = {
    MAIN_REFUSES: ["/bin/sh", "-c", "exit 78"],
    MAIN_FAILS: ["/bin/sh", "-c", "exit 1"],
    CHECK_REFUSES: ['--property=ExecStartPre=/bin/sh -c "exit 78"', "/bin/sleep", "600"],
}

#: The seconds before the first read, and the two read limits of the script.
WATCH_SECONDS = "5"
RESTART_READS = 60
REMOVE_READS = 10

#: The flags of the tool that reads a unit file. The file follows them.
VERIFY = ["verify", "--man=no", "--recursive-errors=no"]

#: A unit file of the throwaway tree. The fake tool does not read it.
UNIT_BODY = "[Service]\nExecStart=/opt/creche/bin/one\n"

#: Writes its name and its argv, with a tab between two words.
LOG = """( IFS=$'\\t'; printf '%s\\t%s\\n' "${0##*/}" "$*" ) >> "$PROOF_LOG"
"""

#: Takes only `sudo -n`. With SUDO_ASKS it refuses, as a sudo that needs a
#: password does. It marks the program that it starts.
FAKE_SUDO = f"""#!/usr/bin/env bash
{LOG}
if [[ -n "${{SUDO_ASKS:-}}" ]]; then
  echo "sudo: a password is required" >&2
  exit 1
fi
if [[ "${{1:-}}" != "-n" ]]; then
  echo "the fake sudo takes only -n" >&2
  exit 64
fi
shift
VIA_SUDO=1 exec "$@"
"""

#: Says what process 1 is.
FAKE_PS = f"""#!/usr/bin/env bash
{LOG}
echo "$PROOF_INIT"
"""

#: Waits for nothing.
FAKE_SLEEP = f"""#!/usr/bin/env bash
{LOG}
"""

#: Needs root. A unit that it starts is on the machine: the mark of the
#: removal goes. RUN_FAILS names a unit whose start the fake refuses.
FAKE_RUN = f"""#!/usr/bin/env bash
{LOG}
[[ -n "${{VIA_SUDO:-}}" ]] || {{ echo "systemd-run: not root" >&2; exit 1; }}
for word in "$@"; do
  if [[ "$word" == --unit=* ]]; then
    unit="${{word#--unit=}}"
    [[ "$unit" != "${{RUN_FAILS:-}}" ]] || exit 1
    rm -f "$PROOF_STATE/$unit/removed"
  fi
done
"""

#: `show` prints the first line of the file of a property. While the file
#: holds more lines, the read removes that line, so a second read gets the
#: next value. A property with no file fails. `stop` and `reset-failed` need
#: root. After `reset-failed` a unit reads as `not-found`, unless the test
#: gave it a LoadState.
FAKE_SYSTEMCTL = f"""#!/usr/bin/env bash
{LOG}
case "${{1:-}}" in
  --version)
    printf 'systemd 255 (255.4-1ubuntu8)\\n+PAM +AUDIT\\n'
    ;;
  show)
    property="${{2#--property=}}"
    [[ "$3" == "--value" ]] || exit 64
    file="$PROOF_STATE/$4/$property"
    if [[ "$property" == LoadState && ! -e "$file" && -e "$PROOF_STATE/$4/removed" ]]; then
      echo not-found
      exit 0
    fi
    [[ -f "$file" ]] || {{ echo "no $property of $4" >&2; exit 1; }}
    sed -n 1p "$file"
    rest="$(sed 1d "$file")"
    if [[ -n "$rest" ]]; then
      printf '%s\\n' "$rest" > "$file"
    fi
    ;;
  stop)
    [[ -n "${{VIA_SUDO:-}}" ]] || exit 1
    ;;
  reset-failed)
    [[ -n "${{VIA_SUDO:-}}" ]] || exit 1
    : > "$PROOF_STATE/$2/removed"
    ;;
  *)
    exit 64
    ;;
esac
"""

#: Prints what the test gave for the file, on stderr, and exits with the
#: status that the test gave. With no answer, the file passes.
FAKE_ANALYZE = f"""#!/usr/bin/env bash
{LOG}
for last in "$@"; do :; done
name="${{last##*/}}"
if [[ -f "$PROOF_ANSWERS/$name.out" ]]; then
  cat "$PROOF_ANSWERS/$name.out" >&2
fi
if [[ -f "$PROOF_ANSWERS/$name.status" ]]; then
  exit "$(cat "$PROOF_ANSWERS/$name.status")"
fi
"""

FAKES = {
    "sudo": FAKE_SUDO,
    "ps": FAKE_PS,
    "sleep": FAKE_SLEEP,
    "systemd-run": FAKE_RUN,
    "systemctl": FAKE_SYSTEMCTL,
    "systemd-analyze": FAKE_ANALYZE,
}


@dataclass(frozen=True)
class Run:
    """One run of the script: its exit code, its output and what it called."""

    code: int
    out: str
    err: str
    calls: list[list[str]]

    def of(self, tool: str) -> list[list[str]]:
        """The argv of each call of one fake, in order."""
        return [call[1:] for call in self.calls if call[0] == tool]

    def reads(self, unit: str, name: str) -> int:
        """How many times the script read one property of one unit."""
        return self.of("systemctl").count(["show", f"--property={name}", "--value", unit])


@dataclass(frozen=True)
class Machine:
    """The throwaway tree, and the fake systemd beside it."""

    root: Path
    fakes: Path
    state: Path
    answers: Path
    path: str

    def holds(self, unit: str, **properties: str | list[str]) -> None:
        """What `systemctl show` answers for a unit. A list is one value
        for each read, and the last value stays."""
        (self.state / unit).mkdir(exist_ok=True)
        for name, value in properties.items():
            lines = [value] if isinstance(value, str) else value
            (self.state / unit / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def unit_file(self, name: str, body: str = UNIT_BODY) -> Path:
        path = self.root / "systemd" / name
        path.write_text(body, encoding="utf-8")

        return path

    def tool_says(self, name: str, text: str, status: int) -> None:
        """What `systemd-analyze verify` prints for a unit file, and its exit
        status. `{file}` is the path of the file and `{name}` is its name."""
        said = text.format(file=self.root / "systemd" / name, name=name)
        (self.answers / f"{name}.out").write_text(said, encoding="utf-8")
        (self.answers / f"{name}.status").write_text(f"{status}\n", encoding="utf-8")

    def removed(self) -> set[str]:
        """The units that the script removed after their start."""
        return {unit for unit in TEST_UNITS if (self.state / unit / "removed").exists()}

    def run(self, *args: str, init: str = "systemd", asks: bool = False, fails: str = "") -> Run:
        """Runs the script. `init` is process 1. `asks` makes sudo ask for a
        password. `fails` names a unit that `systemd-run` does not start."""
        log = self.root.parent / "log"
        log.unlink(missing_ok=True)
        done = subprocess.run(
            [str(self.root / PROOF), *args],
            env={
                "PATH": self.path,
                "PROOF_LOG": str(log),
                "PROOF_STATE": str(self.state),
                "PROOF_ANSWERS": str(self.answers),
                "PROOF_INIT": init,
                "RUN_FAILS": fails,
                # A temporary directory inside a checkout must not lend the
                # run that checkout's git state.
                "GIT_CEILING_DIRECTORIES": str(self.root.parent),
                # The script runs git. That child reads no config file of a
                # person and none of the system.
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            | ({"SUDO_ASKS": "1"} if asks else {}),
            cwd=self.root.parent,
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []

        return Run(
            code=done.returncode,
            out=done.stdout,
            err=done.stderr,
            calls=[[word for word in line.split("\t") if word] for line in lines],
        )


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    """A tree with the real script, one unit file and one Markdown file, on
    a machine where the restart rule holds and the tool passes each file."""
    root = tmp_path / "repo"
    fakes = tmp_path / "fakes"
    state = tmp_path / "state"
    answers = tmp_path / "answers"
    for one in (root / "bin", root / "systemd", fakes, state, answers):
        one.mkdir(parents=True)

    shutil.copy2(REPO / PROOF, root / PROOF)
    for name in TOOLS:
        found = shutil.which(name)
        assert found is not None, f"{name} is not on PATH"
        (fakes / name).symlink_to(found)

    for name, body in FAKES.items():
        (fakes / name).write_text(body, encoding="utf-8")
        (fakes / name).chmod(0o755)

    made = Machine(root=root, fakes=fakes, state=state, answers=answers, path=str(fakes))
    made.holds(
        MAIN_REFUSES, ActiveState="failed", SubState="failed", ExecMainStatus="78", NRestarts="0"
    )
    made.holds(MAIN_FAILS, NRestarts="4")
    made.holds(CHECK_REFUSES, NRestarts="3")
    made.unit_file("creche-one.service")
    made.unit_file("AGENTS.md", "# systemd\n")

    return made


def test_the_proof_passes_where_the_restart_rule_holds(machine: Machine) -> None:
    done = machine.run()

    assert done.code == 0, done.out + done.err
    assert (
        "systemd-proof: case 1: the main process exits 78: "
        "ActiveState=failed SubState=failed ExecMainStatus=78 NRestarts=0\n"
    ) in done.out
    assert "systemd-proof: case 2: the main process exits 1: NRestarts=4\n" in done.out
    assert "systemd-proof: case 3: an ExecStartPre= process exits 78: NRestarts=3\n" in done.out
    assert done.out.splitlines()[0] == "systemd-proof: systemd 255 (255.4-1ubuntu8)"
    assert done.out.splitlines()[-1] == "systemd-proof: PASS"


def test_each_unit_holds_the_restart_rule_and_starts_as_root(machine: Machine) -> None:
    """The whole argv of each start. `--no-block`: a start that fails on
    purpose must not fail the call."""
    done = machine.run()
    starts = [["--no-block", f"--unit={unit}", *RESTART_RULE, *STARTS[unit]] for unit in TEST_UNITS]

    assert done.of("systemd-run") == starts
    for start in starts:
        assert ["-n", "systemd-run", *start] in done.of("sudo")


def test_the_proof_tests_the_status_and_the_line_that_the_rust_code_names() -> None:
    """The script cannot read a Rust constant, so it holds a copy of two.
    This pin fails when a copy differs. It also fails when the Rust file no
    longer holds a constant in this form: change the pin with that file."""
    text = RUST_CONFIG.read_text(encoding="utf-8")
    status = RUST_STATUS.search(text)
    line = RUST_LINE.search(text)

    assert status is not None, f"{RUST_CONFIG.name} holds no `EX_CONFIG` in the pinned form"
    assert line is not None, f"{RUST_CONFIG.name} holds no `NO_RESTART_LINE` in the pinned form"
    assert f"--property={line.group(1)}" == RESTART_RULE[-1]
    assert STARTS[MAIN_REFUSES] == ["/bin/sh", "-c", f"exit {status.group(1)}"]
    assert STARTS[CHECK_REFUSES][0] == (
        f'--property=ExecStartPre=/bin/sh -c "exit {status.group(1)}"'
    )


def test_the_proof_reads_case_1_last_and_after_the_wait(machine: Machine) -> None:
    """The unit of case 1 gets each second that the two other units needed
    to show a restart."""
    machine.holds(MAIN_FAILS, NRestarts=["0", "0", "2"])
    done = machine.run()
    counts = [
        call[3] for call in done.of("systemctl") if call[:2] == ["show", "--property=NRestarts"]
    ]

    assert done.code == 0, done.out + done.err
    assert done.of("sleep") == [[WATCH_SECONDS], ["1"], ["1"]]
    assert counts == [MAIN_FAILS, MAIN_FAILS, MAIN_FAILS, CHECK_REFUSES, MAIN_REFUSES]
    assert "case 2: the main process exits 1: NRestarts=2\n" in done.out


#: What case 1 must not read as a pass: (the property, its value).
NOT_STOPPED = [
    ("ActiveState", "activating"),
    ("ActiveState", "active"),
    ("ActiveState", "inactive"),
    ("SubState", "auto-restart"),
    ("ExecMainStatus", "1"),
    ("ExecMainStatus", "0"),
    ("NRestarts", "1"),
    ("NRestarts", "12"),
]


@pytest.mark.parametrize(("name", "value"), NOT_STOPPED, ids=lambda one: str(one))
def test_case_1_fails_for_a_unit_that_did_not_stay_stopped(
    machine: Machine, name: str, value: str
) -> None:
    machine.holds(MAIN_REFUSES, **{name: value})
    done = machine.run()

    assert done.code == 1, done.out + done.err
    assert "systemd-proof: case 1 FAILED" in done.err
    assert "PASS" not in done.out
    # The log holds the three results also for a failed case.
    assert f"{name}={value}" in done.out
    assert "case 2: the main process exits 1: NRestarts=4\n" in done.out
    assert "case 3: an ExecStartPre= process exits 78: NRestarts=3\n" in done.out
    assert machine.removed() == set(TEST_UNITS)
    assert done.of("systemd-analyze") == []


@pytest.mark.parametrize(("unit", "case"), [(MAIN_FAILS, 2), (CHECK_REFUSES, 3)])
def test_a_unit_that_systemd_does_not_start_again_fails_its_case(
    machine: Machine, unit: str, case: int
) -> None:
    machine.holds(unit, NRestarts="0")
    done = machine.run()

    assert done.code == 1, done.out + done.err
    assert f"systemd-proof: case {case} FAILED" in done.err
    assert "FAILED" not in done.err.replace(f"case {case} FAILED", "")
    assert done.reads(unit, "NRestarts") == RESTART_READS
    assert machine.removed() == set(TEST_UNITS)


@pytest.mark.parametrize("unit", TEST_UNITS)
@pytest.mark.parametrize("value", ["", "-1", "+1", "01", "1.0", "1 2", "n/a", "0x1"])
def test_a_restart_count_that_is_no_count_fails(machine: Machine, unit: str, value: str) -> None:
    """An empty value compares as zero in a shell, and that is the pass of
    case 1."""
    machine.holds(unit, NRestarts=value)
    done = machine.run()

    assert done.code == 1, done.out + done.err
    assert f"systemd-proof: {unit}: NRestarts reads '{value}', which is no count" in done.err
    assert machine.removed() == set(TEST_UNITS)


def test_a_property_that_systemd_does_not_give_fails(machine: Machine) -> None:
    (machine.state / MAIN_REFUSES / "SubState").unlink()
    done = machine.run()

    assert done.code == 1, done.out + done.err
    assert "PASS" not in done.out
    assert machine.removed() == set(TEST_UNITS)


@pytest.mark.parametrize("unit", TEST_UNITS)
def test_a_start_that_fails_stops_the_proof_and_removes_each_unit(
    machine: Machine, unit: str
) -> None:
    done = machine.run(fails=unit)
    started = [call[1] for call in done.of("systemd-run")]

    assert done.code == 1, done.out + done.err
    assert started[-1] == f"--unit={unit}"
    assert done.reads(MAIN_REFUSES, "NRestarts") == 0
    # Each start is after the first removal, and one more removal follows.
    assert done.of("systemctl")[-1] == ["reset-failed", TEST_UNITS[-1]]
    assert done.of("sudo").count(["-n", "systemctl", "stop", unit]) == 2


def test_the_proof_removes_what_an_earlier_run_left_before_it_starts_a_unit(
    machine: Machine,
) -> None:
    done = machine.run()
    first_start = done.calls.index(["sudo", "-n", "systemd-run", *done.of("systemd-run")[0]])
    before = [call[1:] for call in done.calls[:first_start] if call[0] == "sudo"]

    for unit in TEST_UNITS:
        assert ["-n", "systemctl", "stop", unit] in before
        assert ["-n", "systemctl", "reset-failed", unit] in before


def test_a_unit_that_stays_on_the_machine_fails_the_proof(machine: Machine) -> None:
    machine.holds(MAIN_FAILS, LoadState="loaded")
    done = machine.run()

    assert done.code == 1, done.out + done.err
    assert f"systemd-proof: {MAIN_FAILS}: LoadState=loaded after the removal" in done.err
    assert done.reads(MAIN_FAILS, "LoadState") == REMOVE_READS
    assert done.of("systemd-analyze") == []


def test_a_unit_that_systemd_drops_late_passes(machine: Machine) -> None:
    machine.holds(CHECK_REFUSES, LoadState=["loaded", "loaded", "not-found"])
    done = machine.run()

    assert done.code == 0, done.out + done.err
    assert done.reads(CHECK_REFUSES, "LoadState") == 3


def test_a_machine_with_another_process_1_fails_before_any_unit(machine: Machine) -> None:
    done = machine.run(init="/sbin/launchd")

    assert done.code == 1, done.out + done.err
    assert "systemd-proof: process 1 is '/sbin/launchd', not systemd" in done.err
    assert done.of("sudo") == []
    assert done.of("systemd-run") == []


def test_a_sudo_that_asks_for_a_password_fails_before_any_unit(machine: Machine) -> None:
    done = machine.run(asks=True)

    assert done.code == 1, done.out + done.err
    assert "systemd-proof: sudo asks for a password" in done.err
    assert done.of("sudo") == [["-n", "true"]]
    assert done.of("systemd-run") == []


@pytest.mark.parametrize("tool", sorted(FAKES))
def test_a_machine_without_one_tool_fails_before_any_unit(machine: Machine, tool: str) -> None:
    (machine.fakes / tool).unlink()
    done = machine.run()

    assert done.code == 1, done.out + done.err
    assert f"systemd-proof: {tool} not on PATH" in done.err
    assert done.calls == []


#: What the tool prints for a unit file, its exit status, and the line of
#: the proof. `{file}` is the path of the unit file, and `{name}` its name.
ABSENT = "{name}: Command {program} is not executable: No such file or directory\n"
ACCEPTED = [
    ("", 0, "verified"),
    (
        ABSENT.format(name="{name}", program="/opt/creche/bin/one"),
        1,
        "the syntax only, because the machine lacks /opt/creche/bin/one",
    ),
    # The check line and the main line of a unit name one program.
    (
        ABSENT.format(name="{name}", program="/root/bin/one") * 2,
        1,
        "the syntax only, because the machine lacks /root/bin/one",
    ),
    (
        ABSENT.format(name="{name}", program="/root/bin/one")
        + ABSENT.format(name="{name}", program="/usr/local/sbin/two"),
        1,
        "the syntax only, because the machine lacks /root/bin/one, /usr/local/sbin/two",
    ),
]


@pytest.mark.parametrize(("text", "status", "line"), ACCEPTED)
def test_a_unit_file_passes_with_no_line_or_with_an_absent_program_only(
    machine: Machine, text: str, status: int, line: str
) -> None:
    machine.tool_says("creche-one.service", text, status)
    done = machine.run()

    assert done.code == 0, done.out + done.err
    assert f"systemd-proof: creche-one.service: {line}\n" in done.out
    assert "systemd-proof: 1 unit files read, 0 refused\n" in done.out


def test_a_template_passes_with_an_absent_program_of_its_instance(machine: Machine) -> None:
    """The tool reads a template as one instance of it, and names that
    instance in its line."""
    machine.tool_says(
        "creche-many@.service",
        "creche-many@i.service: Command /opt/creche/bin/many"
        " is not executable: No such file or directory\n",
        1,
    )
    machine.unit_file("creche-many@.service")
    done = machine.run()

    assert done.code == 0, done.out + done.err
    assert (
        "systemd-proof: creche-many@.service: the syntax only,"
        " because the machine lacks /opt/creche/bin/many\n"
    ) in done.out


UNKNOWN_KEY = "{file}:2: Unknown key name 'ExecStrat' in section 'Service', ignoring.\n"
REFUSED = [
    (UNKNOWN_KEY, 1),
    # A warning of the tool that does not change its exit status.
    (UNKNOWN_KEY, 0),
    ("{name}: Service has no ExecStart=, ExecStop=, or SuccessAction=. Refusing.\n", 1),
    # The program is there, and the tool cannot run it.
    ("{name}: Command /root/bin/one is not executable: Permission denied\n", 1),
    # A failure with no line.
    ("", 1),
    ("", 2),
    # An absent program does not hide a second line.
    (ABSENT.format(name="{name}", program="/opt/creche/bin/one") + UNKNOWN_KEY, 1),
    # A line that names another unit, also a unit whose name starts the same.
    (ABSENT.format(name="other.service", program="/opt/creche/bin/one"), 1),
    (ABSENT.format(name="creche-one.service.d", program="/opt/creche/bin/one"), 1),
    ("Failed to prepare filename {file}: Invalid argument\n", 1),
]


@pytest.mark.parametrize(("text", "status"), REFUSED)
def test_a_unit_file_with_one_other_line_or_a_bare_failure_is_refused(
    machine: Machine, text: str, status: int
) -> None:
    machine.tool_says("creche-one.service", text, status)
    machine.unit_file("creche-two.timer")
    done = machine.run()
    said = text.format(
        file=machine.root / "systemd" / "creche-one.service", name="creche-one.service"
    )

    assert done.code == 1, done.out + done.err
    assert (
        f"systemd-proof: creche-one.service: REFUSED, with the status {status} of the tool:\n{said}"
    ) in done.err
    # One run shows each refused unit: the next file is read too.
    assert "systemd-proof: creche-two.timer: verified\n" in done.out
    assert "systemd-proof: 2 unit files read, 1 refused\n" in done.out
    assert "PASS" not in done.out


def test_a_template_does_not_pass_on_a_line_of_another_template(machine: Machine) -> None:
    machine.unit_file("creche-many@.service")
    machine.tool_says(
        "creche-many@.service",
        ABSENT.format(name="creche-many-more@i.service", program="/opt/creche/bin/many"),
        1,
    )
    done = machine.run()

    assert done.code == 1, done.out + done.err
    assert "systemd-proof: creche-many@.service: REFUSED" in done.err


def test_the_tool_reads_each_file_that_is_no_markdown_file(machine: Machine) -> None:
    """The whole argv of each call. The path is absolute: the tool prints
    that path in each line on the file. The tool starts nothing, so it does
    not run as root."""
    names = ["creche-many@.service", "creche-one.service", "creche-two.timer", "creche.path"]
    for name in names:
        machine.unit_file(name)
    (machine.root / "systemd" / "notes.d").mkdir()
    done = machine.run()

    assert done.code == 0, done.out + done.err
    assert done.of("systemd-analyze") == [
        [*VERIFY, str(machine.root / "systemd" / name)] for name in names
    ]
    assert [call for call in done.of("sudo") if "systemd-analyze" in call] == []
    assert "systemd-proof: 4 unit files read, 0 refused\n" in done.out


def test_a_tree_with_no_unit_file_fails(machine: Machine) -> None:
    (machine.root / "systemd" / "creche-one.service").unlink()
    done = machine.run()

    assert done.code == 1, done.out + done.err
    assert "systemd-proof: no unit file under systemd/" in done.err
    assert done.of("systemd-analyze") == []


@pytest.mark.parametrize("base", ["", "0" * 40, "HEAD"])
def test_a_change_that_git_cannot_read_is_not_unchanged(machine: Machine, base: str) -> None:
    """The throwaway tree is no repository, so git reads no change there.
    The proof is the safe answer. `bin/tests/test_gate_workflow.py` holds
    the table of the changes that git can read, through the scope of CI."""
    done = machine.run("--unchanged", base, "HEAD")

    assert done.code == 1, done.out + done.err
    # The scope asks this on every machine: it needs no systemd.
    assert done.calls == []


def _git(repo: Path, *args: str) -> None:
    """Runs git in the throwaway tree. The root `conftest.py` gives the
    child no git state and no config file of the caller."""
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def _commit_the_tree(machine: Machine) -> None:
    """Makes the throwaway tree a repository with one commit on `main`."""
    _git(machine.root, "init", "-q", "-b", "main")
    _git(machine.root, "add", "-A")
    _git(machine.root, "commit", "-q", "-m", "first")


#: Two words of `--unchanged` of which one names no commit. git reads such a
#: word as an option or as a path. It then compares another pair of trees,
#: finds no change and prints no path.
NO_COMMIT = [
    ["--quiet", "HEAD"],
    ["HEAD", "--quiet"],
    ["--", "HEAD"],
    ["HEAD", "--cached"],
    ["HEAD", "bin"],
]


@pytest.mark.parametrize("words", NO_COMMIT, ids=" ".join)
def test_a_word_that_names_no_commit_is_not_unchanged(machine: Machine, words: list[str]) -> None:
    """The tree is a repository with one commit here. From `HEAD` to `HEAD`
    nothing changes: that call is the control, and it says `unchanged`."""
    _commit_the_tree(machine)

    assert machine.run("--unchanged", "HEAD", "HEAD").code == 0
    done = machine.run("--unchanged", *words)

    assert done.code == 1, done.out + done.err
    assert done.calls == []


def test_a_file_with_the_name_of_a_commit_does_not_change_the_answer(machine: Machine) -> None:
    """A file at the root has the name of the branch. git must read each of
    the two words as a commit, and never as that file."""
    (machine.root / "main").write_text("", encoding="utf-8")
    _commit_the_tree(machine)

    assert machine.run("--unchanged", "main", "main").code == 0


@pytest.mark.parametrize(
    "args",
    [
        ["--unchanged"],
        ["--unchanged", "HEAD"],
        ["--unchanged", "a", "b", "c"],
        ["--tests"],
        ["x"],
        [""],
        ["", "x"],
    ],
    ids=lambda one: " ".join(one) or "an empty word",
)
def test_a_wrong_call_is_a_usage_error_and_starts_nothing(
    machine: Machine, args: list[str]
) -> None:
    """Status 2 is not the answer `unchanged`, so a scope that calls the
    script in a wrong way runs the proof."""
    done = machine.run(*args)

    assert done.code == 2, done.out + done.err
    assert "usage: bin/systemd-proof.sh [--unchanged FROM TO]" in done.err
    assert done.calls == []
