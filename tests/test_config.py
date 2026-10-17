"""Reading config.toml: limits, types, the owner's name, and the detected network."""

import re
import types
from dataclasses import fields

import pytest

from claude_phone import config
from claude_phone.config import Settings


def test_values_written_the_obvious_other_way_are_read_as_their_settings_type(settings_file):
    settings_file.write_text('ring_seconds = "30"\ndesk_ext = 202\ncalendar_sources = "outlook"\n', encoding="utf-8")
    s = config.load()
    assert (s.ring_seconds, s.desk_ext, s.calendar_sources) == (30, "202", ["outlook"])
    assert config.problems() == []


@pytest.mark.parametrize("line, key", [
    ("ring_seconds = true", "ring_seconds"), ('ring_seconds = "soon"', "ring_seconds"),
    ('meeting_alerts = "yes"', "meeting_alerts"), ('owner_name = ["Sam"]', "owner_name"),
    ("calendar_sources = 1", "calendar_sources"), ("repo_ringtones = 3", "repo_ringtones"),
])
def test_a_value_of_the_wrong_type_keeps_the_default_and_says_so(settings_file, line, key):
    settings_file.write_text(line, encoding="utf-8")
    assert getattr(config.load(), key) == getattr(Settings(), key)
    problem, = config.problems()
    assert problem.startswith(f"{key}: expected ") and "the default" in problem


def test_ring_and_answer_time_over_90_seconds_is_flagged(settings_file):
    settings_file.write_text("ring_seconds = 40\nrecord_seconds = 60\n", encoding="utf-8")
    assert config.problems() == ["ring_seconds + record_seconds is 100 s: keep it at 90 or less, or a call can "
                                 "outlast Claude Code's 120 s limit for a tool"]


def test_unknown_settings_are_a_problem_too(settings_file):
    settings_file.write_text("ring_secnds = 30\n", encoding="utf-8")
    assert config.problems() == [f"Not recognised in {config.CONFIG_FILE.name}, so ignored (a typo?): ring_secnds"]


def test_message_seconds_stays_within_its_range(settings_file):
    for given, used in ((5, 30), (300, 300), (9000, 600)):
        settings_file.write_text(f"message_seconds = {given}", encoding="utf-8")
        assert config.load().message_seconds == used


def test_every_setting_is_in_the_example_config():
    example = (config.REPO / "config.example.toml").read_text(encoding="utf-8")
    assert [f.name for f in fields(Settings) if not re.search(rf"\b{f.name}\b", example)] == []


def test_owner_name_sets_tool_suffix_and_sentence_case():
    assert Settings().owner_slug == "user" and Settings().owner == "The user"
    s = Settings(owner_name="Sam Lee")
    assert s.owner_slug == "sam_lee" and s.owner == "Sam Lee"


def test_lan_cidr_follows_detected_ip(monkeypatch):
    monkeypatch.setattr(config, "detect_lan_ip", lambda: "10.1.2.33")
    s = config.load()
    assert (s.lan_ip, s.lan_cidr) == ("10.1.2.33", "10.1.2.0/24")


def test_config_toml_is_parsed_again_only_once_it_changes(settings_file, monkeypatch):
    parsed = []
    parse = config._parse
    monkeypatch.setattr(config, "_parse", lambda raw: parsed.append(raw) or parse(raw))
    config.load(), config.load(), config.load()
    assert len(parsed) == 1
    # The same size, written at once: an mtime alone could miss this.
    settings_file.write_text("callerid_max = 17\n", encoding="utf-8")
    assert config.load().callerid_max == 17
    settings_file.write_text("callerid_max = 30\n", encoding="utf-8")
    assert config.load().callerid_max == 30 and len(parsed) == 3


def test_a_caller_changing_its_settings_leaves_the_next_load_alone(settings_file):
    settings_file.write_text('calendar_sources = ["google"]\n[repo_voices]\nsite = "piper"\n', encoding="utf-8")
    s = config.load()
    s.ring_ext, s.repo_voices["site"] = "999", "kokoro"
    s.calendar_sources.append("outlook")
    again = config.load()
    assert (again.ring_ext, again.repo_voices, again.calendar_sources) == ("200", {"site": "piper"}, ["google"])


# --- the voice settings from before `voice = "engine:name"` ---

@pytest.mark.parametrize("old, voice", [
    ('tts_provider = "sapi"\nsapi_voice = "Microsoft Zira Desktop"', "windows:Microsoft Zira Desktop"),
    ('sapi_voice = "Microsoft Zira Desktop"', "windows:Microsoft Zira Desktop"),  # sapi was the default provider
    ('tts_provider = "elevenlabs"\nelevenlabs_voice_id = "r1KmysJdVYZjJCm4mL3b"', "elevenlabs:r1KmysJdVYZjJCm4mL3b"),
    ('tts_provider = "elevenlabs_v3"', "elevenlabs_v3"),
    ('tts_provider = "sapi"', ""),  # the Windows default voice, as an empty voice already is
    ('voice = "windows"\nsapi_voice = "Microsoft Mark"', "windows:Microsoft Mark"),  # a bare engine took its name
    ('voice = "piper:en_US-ryan-medium"\ntts_provider = "elevenlabs"', "piper:en_US-ryan-medium"),  # the new key wins
])
def test_old_settings_for_claudes_voice_become_voice(settings_file, old, voice):
    settings_file.write_text(old + "\n", encoding="utf-8")
    assert config.load().voice == voice


@pytest.mark.parametrize("old, operator", [
    ('operator_voice = "Microsoft David Desktop"', "windows:Microsoft David Desktop"),
    ('operator_provider = "elevenlabs"\noperator_voice = "r1KmysJdVYZjJCm4mL3b"', "elevenlabs:r1KmysJdVYZjJCm4mL3b"),
    ('operator_provider = "elevenlabs"', "elevenlabs"),  # its own default voice, not Zira's name
    ('operator_voice = "kokoro:af_bella"', "kokoro:af_bella"),
    ("", "windows:Microsoft Zira Desktop"),
])
def test_old_operator_settings_become_operator_voice(settings_file, old, operator):
    settings_file.write_text(old + "\n", encoding="utf-8")
    assert config.load().operator_voice == operator


@pytest.mark.parametrize("old, whisper", [
    ('whisper_provider = "sapi"\nwhisper_voice = "Microsoft Zira Desktop"', "windows:Microsoft Zira Desktop"),
    ('whisper_provider = "kokoro"', "kokoro"),
    ('whisper_voice = "Microsoft Zira Desktop"', ""),  # ignored without a provider, as it always was
    ('whisper_voice = "piper:en_GB-alba-medium"', "piper:en_GB-alba-medium"),
])
def test_old_whisper_settings_become_whisper_voice(settings_file, old, whisper):
    settings_file.write_text(old + "\n", encoding="utf-8")
    assert config.load().whisper_voice == whisper


@pytest.mark.parametrize("old, key, want", [
    # An engine without a name spoke with sapi_voice or elevenlabs_voice_id: that name comes along.
    ('whisper_provider = "sapi"\nsapi_voice = "Microsoft Mark"', "whisper_voice", "windows:Microsoft Mark"),
    ('whisper_provider = "elevenlabs"\nelevenlabs_voice_id = "XYZ"', "whisper_voice", "elevenlabs:XYZ"),
    ('operator_provider = "elevenlabs"\nelevenlabs_voice_id = "XYZ"', "operator_voice", "elevenlabs:XYZ"),
    ('sapi_voice = "Microsoft Mark"\n[repo_voices.a]\nprovider = "sapi"', "repo_voices",
     {"a": "windows:Microsoft Mark"}),
    # Not old at all: a mistyped engine stays as written (and is reported), an empty operator stays empty.
    ('operator_voice = "pipr:en_US-x"', "operator_voice", "pipr:en_US-x"),
    ('whisper_voice = "kokro:bf_emma"', "whisper_voice", "kokro:bf_emma"),
    ('operator_voice = ""', "operator_voice", ""),
    ('operator_voice = "  Microsoft David Desktop "', "operator_voice", "windows:Microsoft David Desktop"),
])
def test_old_names_come_along_and_new_values_are_left_as_written(settings_file, old, key, want):
    settings_file.write_text(old + "\n", encoding="utf-8")
    assert getattr(config.load(), key) == want


@pytest.mark.parametrize("old", [
    'tts_provider = "elevenlabs"\nelevenlabs_voice_id = "X"\noperator_voice = "Microsoft David Desktop"',
    'whisper_provider = "sapi"\nsapi_voice = "Microsoft Mark"\n[repo_voices.a]\nprovider = "piper"\nvoice = "v"',
    'voice = "piper:x"\noperator_voice = "kokoro:af_bella"\nwhisper_voice = ""\n[repo_voices]\na = "windows"',
])
def test_translating_twice_is_translating_once(old):
    once = config.migrate_legacy(config.tomllib.loads(old))
    assert config.migrate_legacy(once) == once


def test_a_mistyped_engine_and_an_old_value_of_the_wrong_type_are_reported(settings_file):
    settings_file.write_text('operator_voice = "pipr:en_US-x"\ntts_provider = 5\n[repo_voices]\na = "kokro"\n',
                             encoding="utf-8")
    problems = config.problems()
    assert "operator_voice: there's no voice engine called 'pipr'" in "\n".join(problems)
    assert "repo_voices.a: there's no voice engine called 'kokro'" in "\n".join(problems)
    assert "tts_provider: expected text, not 5; ignored" in "\n".join(problems)
    assert config.load().voice == ""


def test_an_old_operator_or_whisper_name_is_noted_as_old_too(settings_file):
    settings_file.write_text('operator_voice = "Microsoft David Desktop"\n', encoding="utf-8")
    problem, = config.problems()
    assert problem.startswith("Old voice settings (operator_voice) still work") and "`phone config migrate`" in problem


def test_the_voice_engines_are_the_ones_tts_has():
    from claude_phone import tts
    assert set(config.VOICE_ENGINES) == set(tts.PROVIDERS)


def test_old_repo_voice_tables_become_engine_name(settings_file):
    settings_file.write_text('[repo_voices.site]\nprovider = "piper"\nvoice = "en_US-ryan-medium"\n'
                             '[repo_voices.api]\nprovider = "sapi"\n'
                             '[repo_voices.odd]\nvoice = "x"\n', encoding="utf-8")  # no provider: Claude's voice
    assert config.load().repo_voices == {"site": "piper:en_US-ryan-medium", "api": "windows"}


def test_old_voice_settings_are_noted_not_reported_as_typos(settings_file):
    settings_file.write_text('tts_provider = "sapi"\n[repo_voices.site]\nprovider = "piper"\n', encoding="utf-8")
    problem, = config.problems()
    assert problem.startswith("Old voice settings (tts_provider, repo_voices tables) still work")
    assert "`phone config migrate`" in problem


def test_the_lan_is_looked_up_at_most_every_30_seconds(monkeypatch):
    looked, clock = [], [1000.0]
    monkeypatch.setattr(config, "detect_lan_ip", lambda: looked.append(1) or "10.1.2.33")
    monkeypatch.setattr(config, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    config.load(), config.load()
    clock[0] += config.LAN_CACHE_S - 1
    config.load()
    assert len(looked) == 1
    clock[0] += 1
    config.load()
    assert len(looked) == 2


def test_a_lan_address_not_found_is_looked_for_again_at_once(monkeypatch):
    # Just woken from sleep, or Wi-Fi not joined yet: the address must show up as soon as there is one.
    found = iter([None, "10.1.2.33"])
    monkeypatch.setattr(config, "detect_lan_ip", lambda: next(found))
    assert config.load().lan_ip == ""
    assert config.load().lan_ip == "10.1.2.33"


def test_a_config_file_held_open_by_another_program_keeps_the_last_settings(settings_file, monkeypatch):
    settings_file.write_text("callerid_max = 30\n", encoding="utf-8")
    assert config.load().callerid_max == 30

    def locked(self):
        raise PermissionError(13, "The process cannot access the file")  # an editor or virus scanner has it
    monkeypatch.setattr(type(settings_file), "read_bytes", locked)
    assert config.load().callerid_max == 30
    config.forget()
    with pytest.raises(PermissionError):  # with nothing read yet, there's nothing to fall back on
        config.load()


def test_broken_toml_is_reported_each_time_until_it_is_fixed(settings_file):
    settings_file.write_text("callerid_max = \n", encoding="utf-8")
    for _ in range(2):
        with pytest.raises(config.tomllib.TOMLDecodeError):
            config.load()
    settings_file.write_text("callerid_max = 30\n", encoding="utf-8")
    assert config.load().callerid_max == 30
