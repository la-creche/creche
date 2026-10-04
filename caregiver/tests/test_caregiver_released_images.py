"""`released.py`: the image references a `playpen` release installed.

Until it existed, a `playpen` release swapped a tree nothing read, and the
fleet stayed on the image an installer once wrote into the unit's
environment. These tests hold the reader's five rules, and then prove the
loop and the one-pass command create a sandbox from what a release named.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from caregiver.cli import EXIT_OK, main
from caregiver.driver import FakeDriver, SandboxSpec
from caregiver.egress import EgressConfig
from caregiver.images import SandboxImages
from caregiver.litellm_keys import FakeLiteLLMKeys
from caregiver.loop import LoopConfig, LoopState, images_now, look
from caregiver.reconcile import Actors
from caregiver.released import MAX_BYTES, ReleasedImages, parse_images, read_images
from caregiver.switch import FakeSwitchClient
from caregiver.timers import FakeUnits
from caregiver_helpers import write_registry

REGISTRY = "127.0.0.1:5000"
BASE = f"{REGISTRY}/agent-sandbox@sha256:{'a' * 64}"
PYTHON = f"{REGISTRY}/agent-sandbox-python@sha256:{'b' * 64}"
LATER = f"{REGISTRY}/agent-sandbox@sha256:{'c' * 64}"
BOTH = f"base={BASE}\npython={PYTHON}\n"

#: What the flags carry: the image an installer built.
GIVEN = SandboxImages(base="sha256:deadbeef")


# -- the file's shape ---------------------------------------------------------


def test_two_lines_are_two_flavors() -> None:
    assert parse_images(BOTH) == SandboxImages(base=BASE, python=PYTHON)


def test_a_host_with_one_flavor_names_one() -> None:
    assert parse_images(f"base={BASE}\n") == SandboxImages(base=BASE, python="")


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"python={PYTHON}\n",
        f"base={REGISTRY}/agent-sandbox:0.9.0\n",
        f"base={REGISTRY}/agent-sandbox@sha256:{'a' * 63}\n",
        f"base={BASE}\nbase={LATER}\n",
        f"base={BASE}\ngpu={PYTHON}\n",
        f"base={BASE} --privileged\n",
        f"base {BASE}\n",
        f"base={BASE}\npython=\n",
        "base=sha256:deadbeef\n",
    ],
)
def test_a_file_that_is_not_one_is_refused_whole(text: str) -> None:
    """A tag can move, an unknown flavor is a file this code does not
    know, and a half-read file is a fleet on two releases."""
    assert parse_images(text) is None


# -- the read -----------------------------------------------------------------


def test_no_file_reads_as_nothing(tmp_path: Path) -> None:
    assert read_images(tmp_path / "images.env") is None


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text(BOTH, encoding="utf-8")
    link = tmp_path / "images.env"
    link.symlink_to(real)

    assert read_images(link) is None


def test_a_file_over_the_cap_is_not_read(tmp_path: Path) -> None:
    path = tmp_path / "images.env"
    path.write_text(BOTH + "\n" * MAX_BYTES, encoding="utf-8")

    assert read_images(path) is None


def test_a_directory_is_not_a_file(tmp_path: Path) -> None:
    path = tmp_path / "images.env"
    path.mkdir()

    assert read_images(path) is None


# -- the memory ---------------------------------------------------------------


def test_a_host_with_no_release_runs_on_the_flags(tmp_path: Path) -> None:
    released = ReleasedImages(tmp_path / "images.env")

    assert released.current(GIVEN) == GIVEN


def test_a_released_file_wins_over_the_flags(tmp_path: Path) -> None:
    path = tmp_path / "images.env"
    path.write_text(BOTH, encoding="utf-8")

    assert ReleasedImages(path).current(GIVEN) == SandboxImages(base=BASE, python=PYTHON)


def test_a_file_that_goes_away_keeps_the_last_good_answer(tmp_path: Path) -> None:
    """A switch renames the tree away for a moment. Read as "no release",
    that moment would replace every sandbox twice."""
    path = tmp_path / "images.env"
    path.write_text(BOTH, encoding="utf-8")
    released = ReleasedImages(path)
    released.current(GIVEN)

    path.unlink()

    assert released.current(GIVEN) == SandboxImages(base=BASE, python=PYTHON)


def test_a_file_that_stops_reading_keeps_the_last_good_answer(tmp_path: Path) -> None:
    path = tmp_path / "images.env"
    path.write_text(BOTH, encoding="utf-8")
    released = ReleasedImages(path)
    released.current(GIVEN)

    path.write_text("base=latest\n", encoding="utf-8")

    assert released.current(GIVEN) == SandboxImages(base=BASE, python=PYTHON)


def test_the_next_release_is_read_on_the_next_pass(tmp_path: Path) -> None:
    path = tmp_path / "images.env"
    path.write_text(BOTH, encoding="utf-8")
    released = ReleasedImages(path)
    released.current(GIVEN)

    path.write_text(f"base={LATER}\n", encoding="utf-8")

    assert released.current(GIVEN) == SandboxImages(base=LATER, python="")


# -- the loop and the one-pass command -------------------------------------------


def _created(driver: FakeDriver) -> list[str]:
    specs = [call.args[0] for call in driver.calls if call.op == "create"]

    return [spec.image for spec in specs if isinstance(spec, SandboxSpec)]


def test_a_pass_creates_the_sandbox_from_the_released_image(tmp_path: Path) -> None:
    path = tmp_path / "images.env"
    path.write_text(BOTH, encoding="utf-8")
    write_registry(tmp_path / "registry")
    driver = FakeDriver()
    config = LoopConfig(
        registry_root=tmp_path / "registry",
        state_root=tmp_path / "state",
        image=GIVEN.base,
        released=ReleasedImages(path),
    )
    actors = Actors(driver, FakeLiteLLMKeys(), FakeSwitchClient(), EgressConfig(), FakeUnits())

    look(config, actors, LoopState())

    assert _created(driver) == [BASE]


def test_a_manager_with_no_released_file_uses_the_flags(tmp_path: Path) -> None:
    config = LoopConfig(
        registry_root=tmp_path / "registry", state_root=tmp_path / "state", image=GIVEN.base
    )

    assert images_now(config) == GIVEN


def _reconcile_once(tmp_path: Path, driver: FakeDriver, *extra: str) -> int:
    return main(
        [
            "reconcile-once",
            str(tmp_path / "registry"),
            "chat",
            "--image",
            GIVEN.base,
            "--state-root",
            str(tmp_path / "state"),
            "--write",
            *extra,
        ],
        driver=driver,
        litellm=FakeLiteLLMKeys(),
        switch=FakeSwitchClient(),
        units=FakeUnits(),
    )


def test_the_one_pass_command_reads_the_file_it_is_given(tmp_path: Path) -> None:
    path = tmp_path / "images.env"
    path.write_text(BOTH, encoding="utf-8")
    write_registry(tmp_path / "registry")
    driver = FakeDriver()

    assert _reconcile_once(tmp_path, driver, "--released-images", str(path)) == EXIT_OK
    assert _created(driver) == [BASE]


def test_the_plan_names_the_image_the_pass_will_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "images.env"
    path.write_text(BOTH, encoding="utf-8")
    write_registry(tmp_path / "registry")

    _reconcile_once(tmp_path, FakeDriver(), "--released-images", str(path))

    printed = capsys.readouterr().out
    assert f"image: {BASE}" in printed
    assert f"image (python): {PYTHON}" in printed


def test_a_state_root_of_its_own_reads_no_released_file(tmp_path: Path) -> None:
    """A scratch run is given its image by its caller. It must not pick up
    what the host's own `playpen` release installed."""
    write_registry(tmp_path / "registry")
    driver = FakeDriver()

    assert _reconcile_once(tmp_path, driver) == EXIT_OK
    assert _created(driver) == [GIVEN.base]
