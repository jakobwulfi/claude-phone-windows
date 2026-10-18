import sqlite3

from claude_phone import config, registry


def test_state_is_kept_by_key_and_cleared_with_none():
    assert registry.get_state("dnd_ours") is None
    registry.set_state("dnd_ours", "2026-10-17T15:30:00+02:00")
    assert registry.get_state("dnd_ours") == "2026-10-17T15:30:00+02:00"
    registry.set_state("dnd_ours", None)
    assert registry.get_state("dnd_ours") is None


def test_a_database_from_before_the_state_table_gets_it_on_connect(monkeypatch):
    old = sqlite3.connect(config.DB_PATH)  # as an older version left it: version 1, no state table
    old.executescript(";".join(registry.MIGRATIONS[0]) + "; PRAGMA user_version = 1;")
    old.close()
    monkeypatch.setattr(registry, "_prepared", None)  # a new process: the daemon after an update
    registry.set_state("k", "v")
    db = sqlite3.connect(config.DB_PATH)
    assert db.execute("PRAGMA user_version").fetchone()[0] == registry.SCHEMA_VERSION
    db.close()
    assert registry.get_state("k") == "v"


def test_lowest_free_extension():
    a = registry.register("a", "repo-a", "main", None, "/a", "tab-a")
    b = registry.register("b", "repo-b", "main", None, "/b", "tab-b")
    assert (a.ext, b.ext) == (301, 302)
    registry.unregister("a")
    assert registry.register("c", "repo-c", "main", None, "/c", "tab-c").ext == 301


def test_new_session_in_same_tab_takes_extension_and_voicemail():
    old = registry.register("old", "repo", "main", None, "/r", "tab")
    registry.add_voicemail("old", old.ext, "repo/main", "Ship it?", None, None)
    new = registry.register("new", "repo", "main", None, "/r", "tab")
    assert new.ext == old.ext
    assert registry.find("old") is None
    assert [v.session_id for v in registry.pending_voicemails()] == ["new"]


def test_find_falls_back_to_tab():
    registry.register("live", "repo", "main", None, "/r", "tab")
    assert registry.find("stale-id", "tab").session_id == "live"


def test_remove_missing():
    registry.register("a", "repo-a", "main", None, "/a", "tab-a")
    registry.register("b", "repo-b", "main", None, "/b", "tab-b")
    registry.record_prompt("a", "Plan it.")
    gone = registry.remove_missing({"tab-b"})
    assert [s.session_id for s in gone] == ["a"]
    assert [s.session_id for s in registry.all_sessions()] == ["b"]
    with registry.connect() as db:  # its last prompt goes too, as when a session ends normally
        assert db.execute("SELECT session_id FROM prompts").fetchall() == []


def test_ringtones_least_used_and_stable():
    rings = [registry.ringtone(f"repo{i}") for i in range(6)]
    assert rings[:5] == list(range(1, 6))  # the T31P plays Ring1-4 and Splash
    assert rings[5] == 1
    assert registry.ringtone("repo3") == 4


def test_a_ring_the_phone_has_no_sound_for_is_given_again():
    with registry.connect() as db:  # assigned when eight rings were handed out
        db.execute("INSERT INTO ringtones (repo, ring) VALUES ('old', 7), ('kept', 1)")
    assert registry.ringtone("old") == 2
    assert registry.ringtone("old") == 2
