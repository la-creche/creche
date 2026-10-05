"""How the index builder starts, refuses, takes a signal and ends.

No test that calls the program as a function can see these: an exit status,
a variable of the environment, a signal, a kill, a store that the next run
cannot read. Each scenario names the rule that it holds, and each assertion
reads a process boundary.

CONTRACT-QUESTION: no contract names an exit status of `index-scope`.
Reading taken: status 2 for a command line that the program refuses, which
is the status that it returns today. For each other run that the program
does not complete, any status that is not 0: a start with no TEI address, a
preflight that fails, a signal, and a store that the program cannot read.
The two index units are `oneshot` units of a timer, so no unit starts the
program again because of a status. A change to one fixed status costs one
assertion in each scenario here.
"""

from __future__ import annotations

import signal
from pathlib import Path

from proc_harness import LOOPBACK
from proc_library import (
    CANARY,
    EXIT_OK,
    EXIT_USAGE,
    FIRST_WORD,
    LAN_ADDRESS_ENV,
    MEETING,
    META_MODEL,
    SECOND_WORD,
    TEI_PORT,
    TEI_URL_ENV,
    UNKNOWN_MODEL,
    LibraryStack,
    Profile,
    index_words,
    report_of,
    write_note,
    write_vault,
)
from proc_standins import TEI, TEI_DIMS, TEI_NO_MODEL_ID, tei_calls, tune

#: A host name that no resolver knows: the top-level name `invalid` is
#: reserved for that.
NO_SUCH_HOST = "tei.invalid"

#: A count of values that is not the count of the store.
OTHER_DIMS = "384"

#: A store file that is no SQLite database.
NO_DATABASE = b"this file is no database\n" * 64

#: How long the program has to end after a signal. Longer than the stop
#: limit of each unit of this repository.
STOP_DEADLINE_S = 30.0


# ------------------------------------------------------------ the command line


def test_a_wrong_count_of_arguments_is_refused(library: LibraryStack) -> None:
    """The command takes two words or three. No word, one word and four words are refused."""
    scope = library.vault()
    write_vault(scope)
    whole = index_words(scope, library.index_dir(), Profile.VAULT)

    codes = [library.run_words(words).exit_code for words in ([], whole[:1], [*whole, "more"])]

    assert codes == [EXIT_USAGE, EXIT_USAGE, EXIT_USAGE]
    assert not library.store().path.exists()


def test_an_unknown_profile_is_refused(library: LibraryStack) -> None:
    """The profile is `vault` or `code`, in lower case. The program guesses no other word."""
    scope = library.vault()
    write_vault(scope)
    words = index_words(scope, library.index_dir())

    codes = [library.run_words([*words, profile]).exit_code for profile in ("prose", "VAULT")]

    assert codes == [EXIT_USAGE, EXIT_USAGE]
    assert not library.store().path.exists()


def test_a_scope_that_is_no_directory_is_refused(library: LibraryStack) -> None:
    """A corpus path with no directory behind it: no such path, and a file."""
    scope = library.vault()
    write_vault(scope)
    absent = library.vault("absent")

    codes = [
        library.run_index(corpus, library.index_dir()).exit_code
        for corpus in (absent, scope / MEETING)
    ]

    assert codes == [EXIT_USAGE, EXIT_USAGE]
    assert not library.store().path.exists()


# -------------------------------------------------------- the address of TEI


def test_main_stops_before_any_call_without_an_address(library: LibraryStack) -> None:
    """Fail closed: no default address. The message names the variable to set."""
    scope = library.vault()
    write_vault(scope)

    done = library.run_index(scope, library.index_dir(), env={})

    assert done.exit_code != EXIT_OK
    assert LAN_ADDRESS_ENV in done.stderr
    assert tei_calls(library.tree) == []
    assert not library.store().path.exists()


def test_an_explicit_tei_url_still_wins(library: LibraryStack) -> None:
    """With both variables, the program dials the URL and never the address."""
    scope = library.vault()
    write_vault(scope)
    env = {TEI_URL_ENV: library.tei_url, LAN_ADDRESS_ENV: NO_SUCH_HOST}

    done = library.run_index(scope, library.index_dir(), env=env)

    assert done.exit_code == EXIT_OK, done.stderr
    assert report_of(done).indexed == 2
    assert library.info_calls() == 1


def test_the_url_is_built_from_the_lan_address(library_prepared: LibraryStack) -> None:
    """With the address alone, the program dials the port of TEI on that address.

    No test can listen on that port of a LAN address. So the address is a
    name that no resolver knows, and the scenario reads the target that the
    program names when the call fails.
    """
    library = library_prepared
    scope = library.vault()
    write_vault(scope)

    done = library.run_index(scope, library.index_dir(), env={LAN_ADDRESS_ENV: NO_SUCH_HOST})

    assert done.exit_code != EXIT_OK
    assert f"{NO_SUCH_HOST}:{TEI_PORT}" in done.stderr
    assert not library.store().path.exists()


# --------------------------------------------------------------- the preflight


def test_an_unreachable_tei_leaves_the_store_untouched(library: LibraryStack) -> None:
    """`library/AGENTS.md`, "Run it": the program asks TEI first, and stops when none answers."""
    scope, built = _built(library)
    write_note(scope / MEETING, "The meeting moved to Thursday.")
    nothing_listens = f"http://{LOOPBACK}:{library.supervisor.free_port()}"

    done = library.run_index(scope, library.index_dir(), env={TEI_URL_ENV: nothing_listens})

    assert done.exit_code != EXIT_OK
    assert library.store().path.read_bytes() == built


def test_a_tei_with_other_dimensions_is_refused(library: LibraryStack) -> None:
    """A vector of another length is a vector of another model. The canary shows it."""
    scope, built = _built(library)
    first_calls = len(library.embedded())
    write_note(scope / MEETING, "The meeting moved to Thursday.")
    tune(library.tree, TEI, TEI_DIMS, OTHER_DIMS)

    done = library.run_index(scope, library.index_dir())

    assert done.exit_code != EXIT_OK
    assert library.embedded()[first_calls:] == [[CANARY]]
    assert library.store().path.read_bytes() == built


def test_a_tei_that_names_no_model_is_recorded_as_unknown(library: LibraryStack) -> None:
    scope = library.vault()
    write_vault(scope)
    tune(library.tree, TEI, TEI_NO_MODEL_ID)

    done = library.run_index(scope, library.index_dir())

    assert done.exit_code == EXIT_OK, done.stderr
    assert library.store().meta()[META_MODEL] == UNKNOWN_MODEL


# ------------------------------------------------------- a signal and a kill


def test_sigterm_during_a_run_leaves_the_store_untouched(library: LibraryStack) -> None:
    """A run that a signal ends publishes nothing, and the next run completes."""
    scope, built = _built(library)
    child = library.start_held_update(scope, library.index_dir())

    child.send(signal.SIGTERM)
    exit_code = child.wait(STOP_DEADLINE_S)
    after_signal = library.store().path.read_bytes()
    library.release_hold()
    next_run = library.run_index(scope, library.index_dir())

    assert exit_code != EXIT_OK
    assert after_signal == built
    assert next_run.exit_code == EXIT_OK, next_run.stderr
    assert _new_words_match(library)


def test_sigkill_during_a_run_leaves_the_store_untouched(library: LibraryStack) -> None:
    """Each index unit ends a run that is too long with SIGKILL. No code of the program runs then.

    The kill comes after the program has the vectors of the first changed
    note. The store has its old bytes, and the next run starts from that
    store, not from what the killed run left.
    """
    scope, built = _built(library)
    child = library.start_held_update(scope, library.index_dir())

    child.send(signal.SIGKILL)
    exit_code = child.wait(STOP_DEADLINE_S)
    after_kill = library.store().path.read_bytes()
    library.release_hold()
    next_run = library.run_index(scope, library.index_dir())

    assert exit_code == -signal.SIGKILL
    assert after_kill == built
    assert next_run.exit_code == EXIT_OK, next_run.stderr
    assert report_of(next_run).indexed == 2
    assert _new_words_match(library)


# ------------------------------------------- a store that the run cannot read


def test_a_store_that_is_no_database_stops_the_run(library: LibraryStack) -> None:
    """Fail closed: the program writes no new store over a file that it cannot read."""
    scope = library.vault()
    write_vault(scope)
    store = _write_store(library, NO_DATABASE)

    done = library.run_index(scope, library.index_dir())

    assert done.exit_code != EXIT_OK
    assert store.read_bytes() == NO_DATABASE


def test_an_empty_store_file_stops_the_run(library: LibraryStack) -> None:
    """A file of 0 bytes is a database with no table. It is not a store."""
    scope = library.vault()
    write_vault(scope)
    store = _write_store(library, b"")

    done = library.run_index(scope, library.index_dir())

    assert done.exit_code != EXIT_OK
    assert store.read_bytes() == b""


# -------------------------------------------------------------------- helpers


def _built(library: LibraryStack) -> tuple[Path, bytes]:
    """A vault scope with a store. Returns the scope and the bytes of the store."""
    scope = library.vault()
    write_vault(scope)
    done = library.run_index(scope, library.index_dir())

    assert done.exit_code == EXIT_OK, done.stderr

    return scope, library.store().path.read_bytes()


def _new_words_match(library: LibraryStack) -> bool:
    """Whether the store holds the new word of each note of a held update."""
    store = library.store()

    return store.matches(FIRST_WORD) != [] and store.matches(SECOND_WORD) != []


def _write_store(library: LibraryStack, raw: bytes) -> Path:
    """Put a file in the place of the store before the first run."""
    store = library.store().path
    store.parent.mkdir(parents=True)
    store.write_bytes(raw)

    return store
