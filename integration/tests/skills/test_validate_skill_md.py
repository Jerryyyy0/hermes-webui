"""SKILL.md format validation for custom uploads."""

from integration.skills.validate import validate_skill_md_content

_VALID = "---\nname: my-skill\ndescription: Does something useful.\n---\n# Body\n"


def test_valid_skill_md():
    assert validate_skill_md_content(_VALID) is None


def test_requires_frontmatter():
    err = validate_skill_md_content("# No frontmatter\n")
    assert err and err["status"] == 400


def test_requires_name():
    err = validate_skill_md_content("---\ndescription: x\n---\n")
    assert err and "name" in err["error"].lower()


def test_requires_description():
    err = validate_skill_md_content("---\nname: x\n---\n")
    assert err and "description" in err["error"].lower()


def test_rejects_system_name():
    err = validate_skill_md_content("---\nname: hermes\ndescription: x\n---\n")
    assert err and err["status"] == 400


def test_rejects_empty_content():
    err = validate_skill_md_content("   ")
    assert err and err["status"] == 400
