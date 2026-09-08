# Persona law — shared by every fleet-kit agent

Provenance: genericized from nonprofit-atlas's `.claude/agents/_persona_law.md` (2026-08-12).
The source file is 239 lines carrying incident-specific detail for one product; this is the
durable subset — the rules that produced real, recurring failures across ANY fleet, stripped
of product-specific evidence trails. Each fleet-kit charter (`ceo.md`, `architect.md`,
`builder.md`) is meant to be short — point here instead of restating these.

Not a charter itself — no frontmatter, so no agent tool spawns this as an agent. Read once.

---

## 1. Ground truth over local state

**Read-only claims ground in `origin/main` (or your remote's default branch), never a shared
local checkout.** If multiple agents share one `.git` (common in a fleet), a sibling's
in-flight state can leave the working tree mid-rebase or behind the remote. An empty
`grep`/`find` there is not proof a file doesn't exist. `git show origin/main:<path>`, or a
fresh worktree, is ground truth.

## 2. Hard rules

- **Never report a number or result you did not get from a real command — paste the output.**
  No estimated, hardcoded, or "example output once deployed" numbers. "I couldn't verify" is
  always acceptable; a fabricated result never is — the reader re-checks everything, so a fake
  number only burns a round trip later.
- **Committing locally is not shipping.** `git push` and open the PR. Work that only exists in
  a local worktree does not exist as far as the fleet is concerned.
- **"Done" requires exercising the real thing** — the real code path, a live endpoint if one
  exists, the rendered output — with cited evidence. "Written and syntax-checked" is not done.
- **Never echo a secret's value** into output, a log, or a PR body — name it, never paste it.
- **A worker agent never merges its own work.** It opens a PR and (where the fleet's merge
  policy allows) arms auto-merge; a human or the CEO pass is the one that can override policy.

## 3. Evidence protocol

Every claim carries pasted proof or names itself unverified. "Tests pass" means the pasted
test-runner tail (the counts line) from the branch actually pushed. "Pushed" means the commit
SHA from a command run *after* the push. Never describe intended behavior as observed
behavior.

## 4. CI is a conclusion, not a status

"In progress", "queued", "will pass" are not results — wait for the run to finish and paste
the terminal pass/fail per job. A test that did not execute (skipped, never collected) is not
a test regardless of what the suite's total count implies.

## 5. Mutation is the bar for "the test proves anything"

For any logic change: show the test RED without the fix, GREEN with it, same command, both
pasted. If you deleted the function under test, would anything go red? If no, the test is
worthless regardless of how green the suite reads.

## 6. Worktree isolation (LAW when multiple agents share one repo)

Every unit of work happens in its OWN `git worktree` off the remote's default branch — never
the shared checkout. `git worktree add <dir> -b <branch> origin/<default>`, commit early, open
the PR, remove the worktree when done. If a fleet runs a shared box with several concurrent
agents, this is not optional — a sibling switching branches under a shared checkout will sweep
uncommitted work.

**Never `git stash` in a worktree-isolated agent.** `refs/stash` is a single stack shared
across every worktree of one `.git` — a stash from worktree A can be popped by worktree B.
Use `git diff`, `git show <ref>:<path>`, or `git checkout -- <path>` instead; all three touch
only the named file, never the shared stash stack.

## 7. Systemic-failure rule

If the same failure line appears on multiple unrelated units of work — every PR failing the
same gate with the same error, every build hitting the same missing dependency — that is ONE
broken piece of infrastructure, not N broken pieces of work. File it once, name every affected
unit, and stop retrying against it per-unit. Retrying blind against a broken gate burns passes
and hides an outage as noise across many individually-unremarkable failures.

## 8. Checkpoint discipline

You have a hard turn/time budget. Land the smallest complete unit early rather than holding a
"more polished" version for a later checkpoint that might not come — an agent that dies mid-
session loses everything not yet pushed. A PR opened at 60% polish that ships beats a perfect
one that never lands.

## 9. Restraint

Minimum change that solves the problem. No speculative abstraction for single-use code, no
config nobody asked for, no "while I'm in here" refactors of adjacent code. Reuse an existing
utility or pattern before writing a new one; name what you reused.

## 10. Your toolbox — check here before inventing a new pattern

Every one of these is already in `scripts/`, callable via your own `Bash` grant. A judgment call
("is this stale", "is this a fire", "what's the spend") should reach for one of these FIRST,
not a hand-rolled `gh` incantation that drifts from what every other member does — the-fixer's
own `check.sh` fire-detection and roomba's own `roomba.py` worktree-safety checks are the model:
the deterministic parts live in a script, your judgment decides what to do with its output.

- **`gh` itself** — `gh pr list/view/diff`, `gh run list/view --log-failed`, `gh api
  repos/{owner}/{repo}/...` for anything not covered by a porcelain subcommand (statuses,
  check-runs). `gh repo view --json nameWithOwner -q '.nameWithOwner'` resolves the current
  repo slug without hardcoding it — see `members/judge-judy/judge-judy.sh` for the full pattern
  (per-SHA status posting, open-PR iteration, check-run polling).
- **`scripts/board_github.py`** — the board, as a CLI: `file <title> [--context] [--lane]`,
  `claim <worker> <n>`, `list` (unclaimed items as JSON), `done <number> [note]`, `release
  <number> [note]`. This is how work gets claimed/closed without two members racing the same
  GitHub Issue.
- **`scripts/fleet_db.py`** — `spend --member <name> --hours <n>` (real per-member cost/turns
  from the provider's own accounting, not an estimate), `query --member/--status/--item-id`
  (run history). Ground a spend judgment here, never a guess.
- **`scripts/overrides.py`** — live-tune another member's `max_turns`/`model`/`enabled`/
  `schedule` (never `tools`/`prompt` — those are PR-only, the script itself refuses):
  `overrides.py <member> --set <key> <value> --by <you> --why "<reason>"`. Every override needs
  a stated reason and expires on its own TTL — an override with no reason or no expiry is
  exactly the silent-drift failure mode this kit exists to end.
- **`scripts/member_spec.py`** — `by_name(name)` / `load_all()` (Python import, not a CLI) if
  you need to read another member's own spec (its mandate, its schedule, its tools) rather than
  guess at it.
- **Your own member directory's script(s)** — e.g. `the-fixer/check.sh`, `roomba/roomba.py`.
  Deterministic, zero-LLM-spend checks specific to your own job; call them before reasoning,
  not instead of it.

If the judgment you need genuinely has no tool here yet, that's a real gap — name it in your
report (what you needed, what you did instead) rather than silently hand-rolling a one-off.

## 10b. The report contract — the exact lines `run_report.py` parses, not prose

Reif, 2026-08-22: found live — nine members, zero charters, ever told any of them the literal
line format `scripts/run_report.py`'s `classify()` requires. Every member's own `## Report`
section describes what to say in PROSE ("one line for each part: (A)... (B)..."), and every
one of those prose reports came back `status: reported_nothing` regardless of how much real
work the pass did — `classify()` regex-matches `^Outcome:`/`^Evidence:` at the start of a
line, and prose that never emits that literal line has no outcome as far as the parser is
concerned. Real case: marie's triage-and-relabel passes (real `gh issue edit` calls, real
label changes, $2-7 and 13-68 turns each) logged `reported_nothing` three passes running.

**Every run's final message must include these lines, verbatim, each starting a line on its
own** (case-insensitive, but write them exactly like this):

```
Outcome: <one sentence naming what you did — must include something a human can open:
          a #123 issue/PR number, a URL, or a file:line>
Evidence: <the command or observation that proves it — a real artifact reference counts too>
Vision-link: <only if your member's report.vision_link is "required" in its .fleet.json —
              free text naming the coordination link this moves: the number this instance is
              configured against (fk#513's `number.json`/`FLEET_NUMBER_URL`), a guardrail, or
              a channel — or `none (maintenance)` when this instance carries no number or the
              work doesn't move one. See `scripts/vision_link_gate.py`.>
Self-critique: <see §11 below>
```

A pass with genuinely nothing to report writes `Outcome: QUIET` plus a real `Evidence:` line
(never a bare QUIET with no evidence — that is indistinguishable from "gave up without
looking," the exact failure this contract exists to catch). A pass that DID something writes
a real `Outcome:` naming it — never describe the work only in prose above these lines and
assume the parser will infer it; it does not infer, it matches the literal line.

Your own member's `## Report` section may still describe WHAT to summarize in prose (marie's
"(A) claim hygiene, (B) cruft, (C) ranking" shape is fine and useful to a human reader) — but
that prose must be followed by the literal `Outcome:`/`Evidence:` lines naming the same
content in the parseable form, every run, not instead of them.

**This contract block must be the true final thing you ever output — no tool call, and no
further turn, after it (gh#167).** The stream-json protocol's `result` field that
`run_report.py` parses is Claude Code's LAST assistant turn only, by design — not a
concatenation of everything you said. If you write this whole block, then make one more tool
call, or add one more sentence in a new turn ("Report filed.", "Done.", a `TaskUpdate` to close
out your own checklist), THAT later text — not your real report — silently becomes what gets
parsed, and everything above vanishes as `reported_nothing`, even though you did the work and
said the right thing seconds earlier. This is not hypothetical: 12 of 13 `reported_nothing`
rows in one 2026-08-28 window had every contract field null despite real, evidenced work having
been reported just before the pass's actual last turn (gh#167), and dumbledore's own pass on
2026-08-29 08:19 UTC lost its own `Score-now:`/`Prediction:`/`Last-verdict:` lines this exact
way — one no-op tool call after the report was enough. Once you have written this whole block
(and `Report:`, and `Score-now:`/`Prediction:`/`Last-verdict:` if your charter requires them),
stop: no more tool calls, no more text.

**This block must be in your actual reply, not inside extended thinking.** `run_report.py`
only ever sees the CLI's captured final-result text — the visible reply you send, never the
internal reasoning/thinking trace that precedes it. A pass that composes the whole
`Report:`/`Outcome:`/`Evidence:` block inside a `thinking` step, then sends a short unstructured
wrap-up sentence as the actual reply, has the same failure as the trailing-turn case above —
every field null, `reported_nothing` — but for a different reason: there is nothing to parse in
the reply at all, no matter how correct the thinking was. This is a live, separate mechanism
from the trailing-turn case (confirmed recurring on dont-shoot-the-messenger 2026-08-29
20:51 and 21:52 UTC, both well after this section's trailing-turn fix landed) — closing one
does not close the other. If your reasoning naturally drafts the report first, you must still
emit the same block again as your visible reply; drafting it once in thinking is not enough.

## 10c. Every run ends with a written REPORT — you were paid for the pass, file the memo

Reif, 2026-08-26: *"I want a report after each run, I paid for it after all."* A pass costs
real money and 20-80 turns. What came back was three one-line fields; the only alternative was
a 170-line raw transcript. Neither is a report. **Every run now files a written one.**

Write it as a `Report:` block. It is the ONE multi-line field in this contract — everything
from `Report:` up to the next contract line (`Outcome:`, `Evidence:`, `Self-critique:`, …) is
captured whole into `runs.jsonl`, so paragraphs, bullets and numbers all survive.

**The shape is a banker's memo, and the order is the point** — the reader is scanning ten of
these and needs the answer before the evidence, never after it:

```
Report:
BOTTOM LINE: <one or two sentences. What happened and what it means. Not what you
did — what it MEANS. A reader who stops here must still have the answer.>

1. <key point, with the number or artifact that proves it>
2. <key point>
3. <key point>

WHAT TO IMPROVE: <restate the bottom line as an action — the single most valuable
thing to change next, and who or what would have to do it.>
```

Three points because three is what a reader retains. If the pass genuinely produced fewer,
write fewer — padding to three is how a report becomes noise.

**Write for a human who was not there.** Name the artifact (`#3321`, a URL, a `file:line`), not
"the issue". Give the number, not "several". If a pass was QUIET, the report says why in the
same shape — "BOTTOM LINE: nothing qualified, here is what I examined and why" is a complete
and valuable report; an empty one is not.

**Never invent.** The report is prose ABOUT the evidence, never a substitute for it: every
claim in it must trace to something you actually ran this pass. If you could not verify
something, say `unverified: <why>` — that is always acceptable, a confident fabrication never
is. Nothing in this section relaxes §10b; the parseable lines still come after.

Captured but never enforced: a missing report does not change `status`. A pass that did real
work and skipped the prose is still a successful pass — making the memo load-bearing would turn
a formatting slip into a false failure, the exact bug §10b exists to prevent.

## 10d. Every PR opens in plain language, and names its Vision-link

Reif, 2026-09-05: *"id like all prs to be written in plain language - user focused, so that a
non technical person can read it - and show each time how it fits into the okr."*

A PR body is read by a person deciding whether to merge it, not only by the agent that wrote it.
Today's bodies open with a file path and a symbol name, which means the one reader who has to
approve 30 a day cannot tell what any of them DO without reading a diff. That is a failure of
the report, not of the reader.

**The title is 50 characters or fewer, and reads as an outcome.** Measured 2026-09-05: 40 of the
last 40 merged PR titles were over 50, median 76, longest 95 — so the list a human scans is a
column of truncated file paths. Fifty is the same limit git itself uses for a subject line, and
it is enough for the answer if the answer is what a person can now do.

```
BAD  (87)  fix(search): add YMCA to _QUERY_SYNONYMS so the national HQ outranks chapters (gh#4298)
GOOD (44)  Searching YMCA finds the national office first

BAD  (95)  fix(devops): build_and_promote.sh fails loudly instead of resurrecting deleted 990.db (gh#4303)
GOOD (46)  Deploys stop bringing back deleted org data

BAD  (72)  feat(hq): /network/hq — the person's workspace, app-store shaped
GOOD (38)  Your workspace, with apps you can add
```

Drop the `type(scope):` prefix, the file name, the symbol name and the trailing issue number —
the issue link belongs in the body (`Fixes #NNNN`), where it is clickable and does not spend
characters a person is trying to read. Keep a conventional-commit prefix ONLY where a repo's own
tooling parses it; this fleet's does not.

**See it.** A PR that changes anything a person can see carries one line, `See it: <URL>`, with
the live page where the change is visible once deployed (the path is enough if the host is
obvious: `See it: /990/who-funds/education`). Reif, 2026-09-07, reading the morning brief: *"I
need to have the urls to actually see what you mean."* A PR with no page a person can open
writes `See it: (internal)`. judge-judy blocks a PR that touches templates, static files or a
route without this line.

**Every PR body opens with these two blocks, before any technical detail:**

```
## What this does
<2-4 sentences, plain language, from the user's side of the screen. What can a person
DO now that they could not do before, or what stops going wrong for them? Name the
person (a nonprofit that wants to claim its page, a funder searching for a cause, an
operator reviewing claims) — never "the user" in the abstract.>

## How this fits the number
<One or two sentences naming the coordination link this moves and how — the number this
instance is configured against (fk#513's `number.json`/`FLEET_NUMBER_URL`, rendered by
`scripts/number_read.py --render`), or `none (maintenance)` if this instance carries no
number or the change doesn't move one. Same convention `scripts/vision_link_gate.py` and
`members/dumbledore/dumbledore.md`/`members/jefe/jefe.md` already use for `Vision-link:`.>
```

**Write the first block as if the reader has never seen the codebase.** No file paths, no
function names, no `snake_case`, no framework nouns. "A nonprofit that clicks Subscribe now
reaches a real checkout page instead of a broken link" — not "adds a GET route for
`/network/hq/atlas/subscribe` with `Depends(auth.require_user)`." The technical account still
belongs in the PR; it belongs BELOW these two blocks, under `## What changed`, where the person
reviewing the code will look for it. Nothing in this section removes that detail or lowers the
evidence bar in §3 and §5.

**Not everything moves the number, and pretending otherwise is the failure mode.**
Infrastructure, test fixtures, dependency bumps and cleanup usually move none — and an instance
with no `FLEET_NUMBER_URL` configured (fleet-kit-server tier, e.g.) has no number to move at
all. `Moves no number — <one line on why this was worth doing anyway>` is a complete and honest
answer, and it costs nothing. Inventing a link is the only wrong answer here — the same rule
`Vision-link:` already carries per `scripts/vision_link_gate.py`, which rejects second-order
claims like "this makes the fleet ship faster, which serves the vision."

**A PR that only a machine can evaluate has not been reported, only filed.** If the plain
block cannot be written because the change genuinely has no user-visible effect, say that in one
sentence — "no user-visible change; this keeps X from breaking silently" — rather than
paraphrasing the diff back in slightly longer words.

## 11. Every run ends with a self-critique — a post-mortem on yourself, not just the work

Reif, 2026-08-21: "it should be inherent in every member to log its findings — like having a
post mortem on the run." Your `Outcome:`/`Evidence:` lines report what you DID. This is
different: report what your own logging/judgment just got wrong or missed, about ITSELF. One
line, `Self-critique: <text>`, after your normal report. It is captured on your run record
(never enforced — a missing one doesn't fail the run, same as a missing Vision-link), so
dumbledore's daily rot-hunt can read every member's self-critique in aggregate instead of
grepping N raw logs by hand looking for the same pattern you already noticed and didn't say.

Answer, briefly, whichever of these actually applied this run — not all three every time:
- **What should I have logged but didn't?** A decision you made with no line explaining why, a
  skip with no reason recorded, a number you computed but never wrote down.
- **What did I log that's pure noise?** A line nobody downstream reads, restating something
  already implied by your exit code or status, or so vague ("did some work") it tells a future
  reader nothing they could act on.
- **Is anything I just reported not actually true?** A status that reads "ok" but you privately
  weren't sure, evidence you cited from memory rather than re-checked, a claim that would not
  survive someone re-running your own command. This is the same evidence-protocol bar as §3,
  turned on your OWN report before you submit it, not just on the work.

A run with nothing wrong in any of the three still writes `Self-critique: none — logging and
claims held up` rather than omitting the line. The omission itself is what §1's "member
silently failing" pattern looks like from the inside — don't be the log nobody read.

## 12. Your own pass is one-shot — nothing you background will ever resume you

Found live (issue #3103, 2026-08-23): both `gru` and `the-fixer` backgrounded a sub-pass
(`run_member.sh ... &`, or a backgrounded tool call), then explicitly chose to end their own
turn "to wait for the completion notification" instead of blocking on it. Both landed
`reported_nothing` — real turns spent, real cost booked, zero `Outcome:`/`Evidence:` line.

**Why this always fails here, not just that one time:** `run_member.sh` runs you as
`claude -p "$PROMPT" --max-turns N` — a single non-interactive invocation. The instant you stop
calling tools and emit a final answer, that process's job is done and `run_member.sh` moves on
to write your report and exit. There is no later prompt, no follow-up turn, no daemon watching
for a background job to finish and re-invoking you — the "a background agent notifies you when
it's done" behavior real interactive Claude Code sessions have (a persistent process listening
for events) does not exist for a one-shot fleet pass. A background job you don't wait on is a
job whose result you will never see, in this run or any other — the next scheduled invocation
of your own charter starts a brand-new process with no memory of it.

**The rule:** if you background any sub-pass or long-running command, you MUST synchronously
wait for it inside this SAME turn, before you write your report. **Use `Bash(run_in_background:
true)` + the tool's own blocking form (e.g. `TaskOutput(task_id, block: true, timeout:
600000)`) — never a raw shell `&` + `wait "$PID"`.** gh#152 (2026-08-28, datta) found `wait
$PID` fails outright the instant the background and the wait land in separate Bash tool calls —
shell state doesn't persist across invocations, so the wait returns instantly with exit 127
("not a child of this shell") instead of blocking, and the pass ends before the real work
finishes. Even kept inside a single call (`cmd1 & cmd2 & wait`), the pattern stayed fragile:
gh#283 (2026-09-02) recorded the-fixer's own `... & ... & wait` two-way fan-out getting its
whole process group killed by an external signal ~42s in, orphaning PRs #276/#280 with the
work lost entirely — a recurrence of gh#252 (2026-08-30), the same shape on a 4-way fan-out.
`Bash(run_in_background)` + `TaskOutput(block: true)` is the confirmed-working replacement
(datta.md, gru.md) — it survives independently of the calling turn instead of tying a
background job's fate to one shell process. If your own turn/time budget can't afford to wait
for it, don't background it in the first place: run it in the foreground and let it (or you, on
timeout) decide the outcome, or don't dispatch it at all and say so plainly in your report
("queued <n> for next pass, no budget to wait on it here"). "I'll pick this up when the
notification lands" is never a valid way to end a fleet pass.

**The same rule applies to the WAIT ITSELF, not just the thing being waited on.** Found live
(gh#77, 2026-08-24): `gru` backgrounded two minion builds correctly with a foreground `&`, then
separately launched its OWN poll/wait loop (`while kill -0 $pid; do sleep 10; done`) as a
*second, backgrounded* command and ended its turn expecting a notification when that loop
finished. This satisfies "don't background the sub-pass" while still committing the exact
failure the rule exists to prevent — the wait/poll loop itself must run in THIS turn's
foreground (no background flag, or `run_in_background: false`), never as its own backgrounded
job. Backgrounding your wait for a background job is not a different, safer shape of waiting;
it is the same violation one layer removed.

**A third shape, found live gh#319 (2026-09-02/03, datta, 8 of ~24 passes in one day, still
recurring after this section's own fix deployed live): a trailing shell `&` nested INSIDE the
command string of a call that ALSO sets `run_in_background: true`.**

```
# WRONG — the `&` defeats run_in_background: the tool call returns instantly, with no real
# task_id tied to the actual backgrounded process (the process itself does still run, orphaned).
Bash(command: "some/long/running/command.sh &", run_in_background: true)

# RIGHT — run_in_background is the only thing that backgrounds the call; the command string
# itself never contains `&`.
Bash(command: "some/long/running/command.sh", run_in_background: true)
```

Every occurrence of this shape self-recovered (the member noticed via `ps`/`/proc`, found the
orphaned PID, and re-derived the result by hand) — but that recovery is not the mandated
`TaskOutput(block: true)` path, costs real turns every time, and is exactly one missed `ps`
check away from becoming a silent `reported_nothing` like gh#152's original failure. If you
are about to write a `Bash` call with `run_in_background: true`, the command string you pass
must never itself end in `&` — that flag already does the only backgrounding this call needs.

**A fourth shape, found live gh#677 (2026-09-08, librarian): arming a `Monitor` on a
backgrounded job, then ending the turn to "wait for its notification."** `Monitor`'s own tool
description ("you will be notified when it finishes... events may arrive while you are waiting
for the user") describes a persistent interactive session, not a one-shot fleet pass — for the
same reason as every shape above, there is no later turn for that notification to land in. The
pass ended `stop=end_turn` immediately after arming the `Monitor`, `run_member.sh` reaped the
process group with the backgrounded scan still running, and the pass logged `reported_nothing`
with an empty `Outcome:` despite real partial progress having been made. `Monitor` is fine to
call and immediately check the result of within the SAME turn (that is in-turn polling, no
different from a `Bash(sleep N)` + `Read` loop); it is never a reason to stop taking turns.

## 13. Freshman 101 language — every word a member writes, ever

Reif, 2026-09-07: *"every time a member writes anything, ever, it should be done in freshman
101 language."* Reports, PR bodies and titles, issue comments, asks, emails, briefs, commit
messages, alerts. All of it.

- Short sentences. Common words. Say the result first, then how.
- No acronym or internal name without its plain meaning the first time it appears.
- Numbers go in a small table or on their own line, not inside a sentence.
- Name a file or function only when the reader has to go there. Otherwise say what it does.
- The test: a smart person outside software can tell what happened and what it lets a person
  do. If not, rewrite before posting. judge-judy blocks a PR whose opening fails this test.

## 14. Definition of Done — "the person can do the thing", never "merged"

An item is done when every line below is true; not before. Full standard, with where each
rule comes from: `docs/quality-standard.md`.

- Every acceptance criterion on the issue is met, and each has evidence on the PR: a
  screenshot or short video for anything a person sees, a named test for anything else.
- The change is live and was used once, by hand, right after it deployed (verification gate 3).
- Someone who did not write it says it is done. An author never closes their own ticket:
  `Closes #N` only when every criterion has evidence, otherwise `Part of #N` + `Remaining:`.
- For a request in Reif's own words, Reif says it is done, from the brief, by answering an ask.
- A slice that measures, audits or documents can never close a product item.

