"""The state-directory rename must carry the user's data across.

The old name is gone from the product, but the directory behind it holds
cookies and recordings a self-hosted user cannot recreate. So the migration
*copies* (never moves), leaves the old directory in place, announces itself,
runs once, and falls back to the old directory if the copy fails — the one
outcome that must never happen is "upgraded, and every login is gone".
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

from happyclaw_browser.settings import reset_state_dir_migration, state_dir


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at a scratch directory and reset migration state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    reset_state_dir_migration()
    return tmp_path


def _seed_legacy(home: Path, name: str = ".octop-browser") -> Path:
    legacy = home / name
    profile = legacy / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("login session", encoding="utf-8")
    return legacy


def test_legacy_state_directory_is_copied_and_announced(fake_home: Path):
    legacy = _seed_legacy(fake_home)

    with pytest.warns(UserWarning, match="copied the state directory"):
        resolved = state_dir()

    assert resolved == fake_home / ".happyclaw-browser"
    copied = resolved / "profiles" / "work" / "Cookies"
    assert copied.read_text(encoding="utf-8") == "login session"
    assert legacy.is_dir(), "the old directory is the user's to delete, not ours"
    assert (legacy / "profiles" / "work" / "Cookies").exists()


def test_migration_runs_once_per_process(fake_home: Path):
    _seed_legacy(fake_home)

    with pytest.warns(UserWarning):
        state_dir()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert state_dir() == fake_home / ".happyclaw-browser"


def test_existing_new_directory_is_never_overwritten(fake_home: Path):
    (fake_home / ".happyclaw-browser" / "profiles").mkdir(parents=True)
    legacy = _seed_legacy(fake_home)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolved = state_dir()

    assert resolved == fake_home / ".happyclaw-browser"
    assert not (resolved / "profiles" / "work").exists()
    assert legacy.is_dir()


def test_the_oldest_release_directory_is_migrated_too(fake_home: Path):
    _seed_legacy(fake_home, ".harness-browser")

    with pytest.warns(UserWarning, match="copied the state directory"):
        resolved = state_dir()

    assert (resolved / "profiles" / "work" / "Cookies").exists()


def test_a_failed_copy_keeps_the_old_directory_in_use(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
):
    legacy = _seed_legacy(fake_home)

    def boom(*_args, **_kwargs):
        raise OSError("profile is locked by a running browser")

    monkeypatch.setattr(shutil, "copytree", boom)

    with pytest.warns(UserWarning, match="could not copy"):
        resolved = state_dir()

    assert resolved == legacy, (
        "falling back to a new empty directory would look like every login was lost"
    )
    assert not (fake_home / ".happyclaw-browser").exists()
    assert not (fake_home / (".happyclaw-browser" + ".migrating")).exists()
