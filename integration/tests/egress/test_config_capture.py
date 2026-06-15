import integration.config as config


def test_capture_dir_default(monkeypatch):
    monkeypatch.delenv("HERMES_EGRESS_CAPTURE_DIR", raising=False)
    assert config.egress_capture_dir() == "/var/log/egress"


def test_capture_dir_override(monkeypatch):
    monkeypatch.setenv("HERMES_EGRESS_CAPTURE_DIR", "/tmp/cap")
    assert config.egress_capture_dir() == "/tmp/cap"


def test_nflog_groups_defaults(monkeypatch):
    monkeypatch.delenv("HERMES_EGRESS_NFLOG_GROUP_ALLOWED", raising=False)
    monkeypatch.delenv("HERMES_EGRESS_NFLOG_GROUP_DENIED", raising=False)
    assert config.egress_nflog_group_allowed() == 100
    assert config.egress_nflog_group_denied() == 200


def test_nflog_group_override_and_bad_value(monkeypatch):
    monkeypatch.setenv("HERMES_EGRESS_NFLOG_GROUP_ALLOWED", "111")
    monkeypatch.setenv("HERMES_EGRESS_NFLOG_GROUP_DENIED", "not-a-number")
    assert config.egress_nflog_group_allowed() == 111
    # 非法值回落默认
    assert config.egress_nflog_group_denied() == 200
