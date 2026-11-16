"""The recorded Forgejo replies of this package name no deployment (root
`AGENTS.md`, "Sanitization").

The test works by allowlist, so it names no person and no host. It reads
each file of `fixtures/forgejo-17.0.1/` and `index.tsv`, each JSON text
inside a JSON string, and each JSON text after a marker of the job log.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, cast

from handover_frp_fixtures import FIXTURES, OWNER, REPOSITORY, SIGNER_FILES, index_row

#: The only logins, user names and author names a fixture may hold.
NAMES = frozenset({OWNER, "operator", "forgejo-actions"})

#: The display names a fixture may hold, beside `NAMES`.
DISPLAY_NAMES = frozenset({"", "Forgejo Actions"})

#: The only email address a fixture may hold, beside an empty one.
EMAIL = "user@example.invalid"

#: The documentation range TEST-NET-1 (RFC 5737), the only addresses.
TEST_NET = "192.0.2."

#: A dotted quad.
ADDRESS_RE = re.compile(r"(?<![0-9.])([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?![0-9.])")

#: A time of day and its UTC offset, or none.
OFFSET_RE = re.compile(r"T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})?")

#: A repository as `<owner>/<name>`, wherever it occurs in a text: a path,
#: a URL or an error text.
REPO_RE = re.compile(rf"([A-Za-z0-9][A-Za-z0-9._-]*)/{REPOSITORY}(?![A-Za-z0-9._-])")

#: An email address with a domain name. An SSH clone URL holds `git@` and an
#: address, and the address check reads that address.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")

#: The keys whose object is a user, a commit author or a committer.
PERSON_KEYS = frozenset({"author", "committer", "tagger", "owner", "creator", "user"})


def _files() -> list[str]:
    return sorted(one.name for one in FIXTURES.iterdir())


def _json_texts(name: str, text: str) -> Iterator[Any]:
    """Each JSON value of a fixture: the whole file, or each marker line of
    the job log whose value is a JSON text."""
    if name.endswith(".json"):
        yield json.loads(text)

        return

    for line in text.splitlines():
        _, _, value = line.partition("=")
        if value.startswith("{"):
            yield json.loads(value)


def _walk(value: Any, key: str = "") -> Iterator[tuple[str, Any, Any]]:
    """Each `(key, value, parent)` of a JSON value, also inside a string
    that holds a JSON text."""
    if isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        for name, item in mapping.items():
            yield name, item, mapping
            yield from _walk(item, name)
    elif isinstance(value, list):
        for item in cast("list[Any]", value):
            yield from _walk(item, key)
    elif isinstance(value, str) and value[:1] in ("{", "["):
        try:
            inner = json.loads(value)
        except ValueError:
            return

        yield from _walk(inner, key)


def _people(value: Any) -> Iterator[dict[str, Any]]:
    """Each object of a user, an author or a committer."""
    for key, item, _ in _walk(value):
        if key in PERSON_KEYS and isinstance(item, dict):
            yield cast("dict[str, Any]", item)


def _faults(name: str, text: str) -> list[str]:
    """Each place where a fixture names something that is not on the
    allowlist."""
    faults = [f"address {one}" for one in ADDRESS_RE.findall(text) if not one.startswith(TEST_NET)]
    faults += [f"offset {one}" for one in OFFSET_RE.findall(text) if one not in ("Z", "+00:00")]
    faults += [f"repository owner {one}" for one in REPO_RE.findall(text) if one != OWNER]
    faults += [f"email {one}" for one in EMAIL_RE.findall(text) if one != EMAIL]
    for value in _json_texts(name, text):
        for key, item, _ in _walk(value):
            if key in ("login", "username") and item not in NAMES:
                faults.append(f"{key} {item}")

            repository = isinstance(item, str) and "/" in item
            if key == "full_name" and repository and item.split("/", 1)[0] != OWNER:
                faults.append(f"full_name {item}")

        for person in _people(value):
            for key in ("name", "full_name"):
                item = person.get(key)
                if item is not None and item not in NAMES | DISPLAY_NAMES:
                    faults.append(f"{key} {item}")

            login, profile = person.get("login"), person.get("html_url")
            linked = isinstance(login, str) and isinstance(profile, str)
            if linked and not str(profile).endswith(f"/{login}"):
                faults.append(f"html_url {profile}")

    return faults


def test_fixtures_are_sanitized() -> None:
    files = _files()
    assert len(files) >= 14

    for name in files:
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert _faults(name, text) == [], name


def test_the_checks_find_what_they_look_for() -> None:
    """Each check refuses a planted fault of its kind."""
    planted = {
        "address": '{"url":"http://10.1.2.3:3000/"}',
        "offset": '{"created":"2026-11-16T00:33:08-08:00"}',
        "repository owner": '{"errors":["in the head repository someone/runner-test"]}',
        "email": '{"email":"a@b.example"}',
        "login": '{"author":{"login":"someone"}}',
        "full_name": '{"repository":{"full_name":"someone/runner-test"}}',
        "name": '{"commit":{"author":{"name":"Some One"}}}',
        "html_url": '{"user":{"login":"operator","html_url":"http://192.0.2.10:3000/x"}}',
        "inner": '{"event_payload":"{\\"sender\\":{\\"login\\":\\"someone\\"}}"}',
    }
    for kind, text in planted.items():
        assert _faults("planted.json", text) != [], kind

    log = '2026-11-16T08:36:56Z M release_body={"author":{"login":"someone"}}'
    assert _faults("planted.txt", log) == ["login someone"]
    assert _faults("planted.json", '{"author":{"login":"operator","name":"operator"}}') == []


def test_each_fixture_has_one_index_row() -> None:
    index = (FIXTURES / "index.tsv").read_text(encoding="utf-8").splitlines()
    named = [line.split("\t")[0] for line in index]
    files = [one for one in _files() if one not in ("index.tsv", "README.md")]

    assert sorted(named) == files
    for name in files:
        fields = index_row(name)
        assert len(fields) == 5, name
        assert fields[1] in ("GET", "POST"), name
        assert fields[2].startswith(f"/repos/{OWNER}/{REPOSITORY}/"), name
        assert fields[3] in ("200", "404"), name

    # The four shared recordings are not copied, so they cannot drift.
    assert not SIGNER_FILES & set(files)
    for name in SIGNER_FILES:
        assert index_row(name)[0] == name
