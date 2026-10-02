"""Fixtures for the stage 1 gate: one whole stack per test, on temp storage.

Every test here spawns real subprocesses and is marked `slow` for that reason.
The mark is applied once, here, so no scenario can forget it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from stack import MODEL, Stack, short_temp_dir, socket_dir_is_usable, supervisor_bundle

_BUILD_HINT = "run `pnpm install && pnpm build` in supervisor/ first"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Contract: `uv run pytest integration/tests -m slow` runs the gate."""
    for item in items:
        item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="session", autouse=True)
def _require_bundle() -> None:
    """Skip rather than fail. A silent pass would be worse than either."""
    bundle = supervisor_bundle()
    if not bundle.exists():
        pytest.skip(f"{bundle} is missing: {_BUILD_HINT}")


@pytest.fixture
def roots(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """The per-test tree, plus a short directory for the two Unix sockets."""
    with short_temp_dir() as sockets:
        socket_dir = Path(sockets)
        if not socket_dir_is_usable(socket_dir):
            pytest.skip(f"this platform refuses a Unix socket under {socket_dir}")

        yield tmp_path, socket_dir


@pytest.fixture
async def stack(roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stack]:
    """A built, serving stack. The client is on `stack.client`."""
    tree, socket_dir = roots
    built = Stack(tree, socket_dir)
    built.build_fixture()

    for name, value in built.sandbox_environ().items():
        monkeypatch.setenv(name, value)

    await built.serve()

    try:
        yield built
    finally:
        await built.close()


def chat_id() -> str:
    """One Open WebUI chat id. A UUID, as Open WebUI mints them."""
    return str(uuid.uuid4())


def message_id() -> str:
    return str(uuid.uuid4())


def owui_headers(chat: str, message: str, **extra: str) -> dict[str, str]:
    """The per-connection custom headers the door reads, named by the door."""
    headers = {"x-owui-chat-id": chat, "x-owui-message-id": message}
    headers.update(extra)

    return headers


def chat_body(text: str, *, stream: bool, system: str | None = None) -> dict[str, object]:
    """One OpenAI chat completion request, as Open WebUI sends it."""
    messages: list[dict[str, object]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})

    messages.append({"role": "user", "content": text})

    return {"model": MODEL, "messages": messages, "stream": stream}


def session_of(chat: str) -> str:
    """Contract 02 §2: an Open WebUI chat becomes `owui-<chat id>`."""
    return f"owui-{chat}"


__all__ = [
    "Stack",
    "chat_body",
    "chat_id",
    "message_id",
    "owui_headers",
    "session_of",
]
