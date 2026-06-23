"""Normalized list item shape tests."""

from integration.skills.list_item_shape import (
    normalize_skill_list_item,
    upstream_catalog_mtime,
)

_LIST_FIELDS = {
    "name",
    "dir_name",
    "display_name",
    "description",
    "category",
    "version",
    "author",
    "installed",
    "hub_installed",
    "custom",
    "disabled",
    "mtime",
}


def test_upstream_catalog_mtime_prefers_mtime_then_updated_at():
    assert upstream_catalog_mtime({"mtime": 10.0, "updated_at": 20.0}) == 10.0
    assert upstream_catalog_mtime({"updated_at": 20.0}) == 20.0
    assert upstream_catalog_mtime({}) is None


def test_normalize_skill_list_item_fills_defaults():
    item = normalize_skill_list_item({"name": "demo"})
    assert set(item) >= _LIST_FIELDS
    assert item["name"] == "demo"
    assert item["dir_name"] == ""
    assert item["display_name"] == ""
    assert item["description"] == ""
    assert item["category"] == ""
    assert item["version"] == ""
    assert item["author"] == ""
    assert item["installed"] is False
    assert item["hub_installed"] is False
    assert item["custom"] is False
    assert item["disabled"] is False
    assert item["mtime"] is None
    assert "updated_at" not in item


def test_normalize_skill_list_item_drops_updated_at():
    item = normalize_skill_list_item({"name": "x", "updated_at": 99.0, "mtime": 99.0})
    assert item["mtime"] == 99.0
    assert "updated_at" not in item
