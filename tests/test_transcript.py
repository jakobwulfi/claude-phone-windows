"""Spotting an Esc interrupt at the end of a session's transcript."""

import json

from claude_phone import transcript


def write(session_id: str, *entries: dict, project: str = "c--repo") -> None:
    folder = transcript.projects_dir() / project
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def user(text) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


ASSISTANT = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Working on it"}]}}
TOOL_RESULT = user([{"type": "tool_result", "tool_use_id": "t1", "content": "rejected"}])
SUMMARY = {"type": "system", "subtype": "away_summary"}


def test_esc_at_a_permission_prompt_is_an_interrupt():
    write("s1", user("start a long task"), ASSISTANT, TOOL_RESULT,
          user([{"type": "text", "text": "[Request interrupted by user for tool use]"}]), SUMMARY)
    assert transcript.interrupted("s1")


def test_esc_during_a_turn_is_an_interrupt():
    write("s2", user("start a long task"), ASSISTANT, user("[Request interrupted by user]"))
    assert transcript.interrupted("s2")


def test_a_turn_in_progress_is_not():
    write("s3", user("start a long task"), ASSISTANT, TOOL_RESULT)
    assert not transcript.interrupted("s3")


def test_a_new_prompt_after_an_interrupt_is_not():
    write("s4", user("[Request interrupted by user]"), user("try again"))
    assert not transcript.interrupted("s4")


def test_a_missing_transcript_is_not():
    assert not transcript.interrupted("nobody")


def test_only_the_tail_of_a_long_transcript_is_read():
    filler = [user("x" * 1000)] * 200  # far more than the tail
    write("s5", *filler, ASSISTANT, user("[Request interrupted by user]"), project="other-project")
    assert transcript.interrupted("s5")
