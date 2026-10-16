"""Reading config.toml: limits, the owner's name, and the detected network."""

from claude_phone import config
from claude_phone.config import Settings


def test_message_seconds_stays_within_its_range(settings_file):
    for given, used in ((5, 30), (300, 300), (9000, 600)):
        settings_file.write_text(f"message_seconds = {given}", encoding="utf-8")
        assert config.load().message_seconds == used


def test_owner_name_sets_tool_suffix_and_sentence_case():
    assert Settings().owner_slug == "user" and Settings().owner == "The user"
    s = Settings(owner_name="Sam Lee")
    assert s.owner_slug == "sam_lee" and s.owner == "Sam Lee"


def test_lan_cidr_follows_detected_ip(monkeypatch):
    monkeypatch.setattr(config, "detect_lan_ip", lambda: "10.1.2.33")
    s = config.load()
    assert (s.lan_ip, s.lan_cidr) == ("10.1.2.33", "10.1.2.0/24")
