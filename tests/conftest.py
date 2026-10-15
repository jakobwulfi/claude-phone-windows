import os
import tempfile

# Point every path at a throwaway home before claude_phone is imported: tests never touch the live
# registry, logs, keychain-backed config or sounds.
os.environ["CLAUDE_PHONE_HOME"] = tempfile.mkdtemp(prefix="cphone-test-")
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="cphone-test-claude-")  # session transcripts

import pytest  # noqa: E402

from claude_phone import config  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.DB_PATH.unlink(missing_ok=True)
    yield
