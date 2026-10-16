"""The operator (dial 0): from what was said to a live session."""

import pytest

from claude_phone.operator import match, spoken_extension
from claude_phone.registry import Session


def sess(ext, repo, branch="main", worktree=None):
    return Session(session_id=f"s{ext}", ext=ext, repo=repo, branch=branch, worktree=worktree, cwd=None,
                   term_id=None, status="idle", started_at=0, updated_at=0)


LIVE = [sess(301, "claude-phone", "master"), sess(302, "app-fe-v2"), sess(303, "site-be-v2"),
        sess(304, "lemon-stand", "feat/ags-2")]
TWINS = [sess(305, "site-be-v2", "main"), sess(306, "site-be-v2", "staging")]


@pytest.mark.parametrize("heard, ext", [
    ("three oh one please", 301), ("put me through to 303", 303), ("the backend", None),
])
def test_spoken_extension(heard, ext):
    assert spoken_extension(heard) == ext


@pytest.mark.parametrize("heard, live, ext", [
    ("The backend.", LIVE, 303), ("the back end repo", LIVE, 303), ("the frontend", LIVE, 302),  # synonyms
    ("claude phone.", LIVE, 301), ("lemon stand", LIVE, 304),  # repo names spoken naturally
    ("the a g s branch", LIVE, 304),  # a spelled-out branch
    ("three oh two", LIVE, 302),  # an extension wins
    ("site backend staging", TWINS, 306),
    ("the weather forecast", LIVE, None),
])
def test_match(heard, live, ext):
    hit, _ = match(heard, live)
    assert (hit.ext if hit else None) == ext


def test_an_ambiguous_match_returns_the_candidates():
    hit, cands = match("site backend", TWINS)
    assert hit is None and {c.ext for c in cands} == {305, 306}
    assert match("the weather forecast", LIVE) == (None, [])
