# CLAUDE.md snippet

Paste into `~/.claude/CLAUDE.md` (all repos) or a repo's `CLAUDE.md`. It's written for `owner_name = "Sam"`,
whose tools are `call_sam` and `notify_sam`: replace `Sam` and `sam` with your name and tool suffix.

```markdown
## Phoning Sam (`call_sam`, `notify_sam`, `transfer_call`)

- `call_sam` asks one question and waits for a spoken answer. Use it only when you are blocked
  on a decision that is genuinely Sam's: product calls, unsettled trade-offs, or approval for something
  risky or irreversible. Never for things you can work out from the code, docs or sensible defaults.
- `notify_sam` reads a short announcement and hangs up. Use it for news Sam would want now with no
  reply needed: a long job finished, something is ready, something broke.
- `transfer_call` puts Sam through by phone to another live session. Use it when you're asked something
  that belongs to another repo; give a one-sentence spoken `reason` and a `handover` note for the target.
- Sam is listening, not reading. One sentence of about 20 words; phrase questions so they can be
  answered out loud ("yes or no", "A or B"). Add `context` only if the question makes no sense
  without it.
- Never read out code, paths, URLs, PR numbers or step-by-step instructions. Put detail on screen
  and say "details are on your screen".
- If `call_sam` isn't answered, a voicemail is left. Carry on with other work or your best
  reversible assumption; the answer arrives later as a message starting
  `[Phone answer to your earlier question]`. A missed `notify_sam` leaves nothing behind.
- Subagents: pass `from_agent` with a short name for yourself.
- If Sam asks by phone for plan mode and entering it waits for approval in the terminal, Sam's Claude Code
  settings lack claude-phone's `PreToolUse` hook. Tell Sam on screen to add it from claude-phone's README
  ("Claude Code wiring"); `phone status` lists any hooks that are missing.
```
