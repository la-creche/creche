"""The site file that the manifest surfaces read: its mode and its place.

No test here builds the vectors.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from handover import site
from vectors.surfaces import manifest

LINES = ("AGENT_OPERATOR_USER=keeper", "AGENT_OPERATOR_HOME=/home/keeper")

#: A mask that lets the group write a new file.
GROUP_WRITE_MASK = 0o002


def test_the_site_file_has_one_mode_under_each_mask() -> None:
    """The reader refuses a site file that its group can write."""
    previous = os.umask(GROUP_WRITE_MASK)
    try:
        with manifest.site_file(LINES) as path:
            mode = stat.S_IMODE(Path(path).stat().st_mode)
            user = site.operator_user()
    finally:
        os.umask(previous)

    assert mode == manifest.SITE_FILE_MODE
    assert user == "keeper"


def test_the_site_file_is_gone_after_the_calls() -> None:
    before = os.environ.get(site.SITE_FILE_ENV)
    with manifest.site_file(LINES) as path:
        assert os.environ[site.SITE_FILE_ENV] == path

    assert os.environ.get(site.SITE_FILE_ENV) == before
    assert not Path(path).exists()


def test_a_host_with_no_site_file_has_a_path_and_no_file() -> None:
    with manifest.site_file(None) as path:
        assert not Path(path).exists()
