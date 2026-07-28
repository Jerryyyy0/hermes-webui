"""list_dir distinguishes missing paths from non-directories."""

from pathlib import Path

import pytest

from api.workspace import list_dir


def test_list_dir_missing_path_says_does_not_exist(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match=r"Path does not exist: tt") as exc:
        list_dir(tmp_path, "tt")
    assert "Not a directory" not in str(exc.value)


def test_list_dir_missing_nested_path_preserves_relative(tmp_path: Path):
    with pytest.raises(
        FileNotFoundError,
        match=r"Path does not exist: notes/github-trending",
    ):
        list_dir(tmp_path, "notes/github-trending")


def test_list_dir_file_is_not_a_directory(tmp_path: Path):
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=r"Not a directory: readme\.txt"):
        list_dir(tmp_path, "readme.txt")


def test_list_dir_real_directory_returns_entries(tmp_path: Path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("x", encoding="utf-8")
    entries = list_dir(tmp_path, "notes")
    assert {e["name"] for e in entries} == {"a.md"}
