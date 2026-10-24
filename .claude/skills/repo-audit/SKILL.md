---
name: repo-audit
description: >
  Quality audit of claude-phone or a named part of it: bugs, security holes, what breaks when several processes
  run at once or the daemon runs for days, risky code without tests, slow paths, and what to delete, merge or
  split. Ranked, each finding explained in plain English. One-shot report, changes nothing. Use for "audit the
  repo", "review the whole codebase", "what can I delete", "is this ready to release".
---

<!-- Adapted from ponytail-audit by Dietrich Gebert (https://github.com/DietrichGebert/ponytail), MIT licensed:
     see LICENSE-ponytail next to this file. -->

Audit the repo like the senior developer who just inherited it and will be paged when it breaks. Order of
importance: correct, safe, holds up in real use, tested, fast, lean. Lean still matters: every extra line must be
read, tested and fixed later. This is a report the user asked for, so give it in full.

## 1. Map first

- Audit what the user names: a module, a folder, the VS Code extension, or the whole repo. Nothing named: the
  whole repo.
- Read `README.md`, `docs/build-notes.md` (why things are built the way they are), `pyproject.toml`, the entry
  points (`cli.py`, `daemon.py` and `daemon_http.py`, `mcp_server.py`, `hooks.py`, `vscode-extension/src`) and the
  tests.
- The load is one person on one PC, but many processes at once: the daemon, one MCP server per Claude session, a
  hook process per event, FreeSWITCH, whisper-server, one extension per VS Code window. They share the SQLite
  registry and files under `~/.claude-phone`. The daemon runs for days. Judge scale against that.
- Trace the main flows end to end: a call coming in, a session calling the user, a transcript typed into a
  terminal, a voicemail, a setting changed. Read those paths fully: input from the phone or network, secrets, data
  writes, background loops, anything shared between processes.
- Go deep where a mistake costs the most, not file by file. Say which parts you did not read.
- Never touch the live daemon, FreeSWITCH, the registry or the user's config while auditing.

## 2. Look for

1. **Bug:** wrong result, crash, missed edge case (empty, zero, last item, time zones, non-ASCII text), callers that
   disagree with what a function returns, the same rule applied differently in two places, `esl.Call` and the
   tests' `FakeCall` drifting apart.
2. **Risk:** security holes (missing token checks, input from the LAN or the phone trusted as is, secrets in code or
   logs), data loss (errors swallowed, writes in the wrong order, a voicemail or setting that can vanish), a hook
   that can break a session, a call that can go silent.
3. **Scale:** fine once, wrong over time or across processes: check-then-write races on the registry or files,
   the same work done by every process, memory or lists that only grow in the daemon, workers or files never
   cleaned up, state kept in one process that others need, anything that blows the 90 s ring-plus-record budget.
4. **Missing test:** risky logic (a branch, a parser, a call flow, security, data writes) with no test that fails
   when it breaks. One good test, not coverage.
5. **Speed:** big slowdowns are problems, above all on the path from a ring or a spoken sentence to the user
   hearing an answer. Small wins are suggestions.
6. **Lean:** code that should not exist or should be smaller.
   - delete: dead code, unused options, flags and config, speculative features
   - reuse: two helpers doing the same thing (keep one, name the path)
   - stdlib / native: the standard library or Windows already does it; a dependency doing what a few lines can do
   - yagni: interface with one implementation, factory with one product, wrapper that only passes calls through
   - merge: near-copies that must change together
   - split: one function or class doing several unrelated jobs, so it is hard to read or test. Split by job, never
     by line count, and never into helpers that exist only to make a function shorter.

## 3. Check before you report

- Every finding needs a concrete case: "this input or situation leads to this wrong result". No case, no finding.
- Before calling code unused, grep the whole tree for it, including tests, fixtures, config, the FreeSWITCH
  template, the extension, and string or dynamic references (tool names built from `owner_name`, CLI subcommands).
- A limit the code or `docs/build-notes.md` already names on purpose is a decision, not a finding, unless real use
  already crosses it.
- Propose the smallest fix that works. Prefer fixes that delete code. Never add layers, frameworks or config the
  problem does not need.
- No style taste, no "consider", no vague worries.

## 4. Output

Very simple English: short sentences, everyday words. Explain a technical term the first time you use it. The
reader may never have seen this code.

Start with `What this repo does:` in two or three sentences, and the load you assumed.

Then the findings in three groups, most important first, skip empty groups:
- **Must fix:** bug, security, data loss, a silent call, breaks in normal use.
- **Should fix:** risky code without a test, real slowness, duplication, a function that mixes jobs, code that
  should not exist.
- **Nice to have:** small speed-ups, shorter forms.

Number findings across all groups, so the user can say "fix 2 and 5". At most 20 findings; if you left smaller
ones out, say how many. Every finding has all four parts, each one or two short sentences:

2. **Orders land on the wrong day** (`billing/close_day.py:L40-52`)
   - **What this is:** At midnight this job closes the day and bills all orders of that day.
   - **Problem:** It takes "today" from the server clock, which runs in UTC. An order placed at 00:30 local time
     is billed on the day before.
   - **Fix:** Compute the day once in the shop's time zone. One line, nothing else changes.
   - **If we skip it:** Late orders show the wrong date, and accounting fixes them by hand.

End with:
- `Verdict:` one line: healthy, or what to fix first.
- `Lean: -<N> lines, -<M> dependencies possible.` when lean findings exist.
- `Not checked:` the parts you did not read or could not run (live calls and the desk phone always belong here).

Nothing found: `What this repo does:`, then `Healthy. Nothing to fix.` and one line on what you checked.

One-shot report, changes no code.
