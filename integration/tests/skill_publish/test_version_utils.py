"""Tests for skill_publish version utils."""

from integration.skill_publish.version_utils import (
    compute_next_version,
    now,
    parse_apply_time,
    semver_gt,
)


def test_compute_next_version_first_publish():
    assert compute_next_version(None) == "1.0.0"
    assert compute_next_version("") == "1.0.0"


def test_compute_next_version_patch_increment():
    assert compute_next_version("1.0.0") == "1.0.1"
    assert compute_next_version("1.0.9") == "1.0.10"
    assert compute_next_version("2.3.4") == "2.3.5"


def test_compute_next_version_invalid_falls_back():
    assert compute_next_version("v1") == "1.0.0"
    assert compute_next_version("1.2") == "1.0.0"
    assert compute_next_version("a.b.c") == "1.0.0"


def test_semver_gt():
    assert semver_gt("1.0.1", "1.0.0")
    assert semver_gt("1.1.0", "1.0.99")
    assert semver_gt("2.0.0", "1.9.9")
    assert not semver_gt("1.0.0", "1.0.0")
    assert not semver_gt("1.0.0", "1.0.1")


def test_semver_gt_lenient_parsing():
    assert semver_gt("1.0", "0.9.9")
    assert not semver_gt("1.0", "1.0.0")


def test_parse_apply_time():
    ts = parse_apply_time("2026-08-17 10:30:00")
    assert ts is not None
    assert ts > 0


def test_parse_apply_time_invalid():
    assert parse_apply_time(None) is None
    assert parse_apply_time("") is None
    assert parse_apply_time("not-a-time") is None
    assert parse_apply_time("2026-08-17T10:30:00") is None


def test_now_monotonic_enough():
    a = now()
    assert a > 1_700_000_000
    assert now() >= a
