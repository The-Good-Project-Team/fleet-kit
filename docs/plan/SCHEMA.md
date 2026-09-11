# The plan file's schema

fk#559 VP review fix 5. This is the one place the `docs/plan/<instance>.md` format is written
down. `scripts/plan_rank.py` (gru's build-eligibility preference tier, gh#572) and
`scripts/messenger_brief.py` (Reif's morning brief, fk#558) both parse against exactly this
contract via one shared resolver, `plan_rank.plan_path_for_instance()` -- see that function's
module docstring for why. `docs/plan/EXAMPLE.md` in this directory is a real, checked-in file
that conforms to it; `scripts/test_plan_rank.py` parses that file, not a fixture string, so a
change to either drifting out of step with the other fails a test.

## Where the file lives

`docs/plan/<instance>.md`, where `<instance>` is `$FLEET_INSTANCE_NAME` with any trailing
`-green`/`-blue` deploy-slot suffix stripped, falling back to `"default"` when the variable is
unset. No file at that path is a fully supported state -- both readers degrade to "no plan"
rather than erroring (see below).

## The `## Bets` section

- A heading whose text is exactly `Bets` (any level `#` through `######`, case-insensitive,
  nothing else on the line) starts the bets section. It runs until the next heading of the
  same level or shallower, or to end of file.
- Every non-blank line inside that section is one bet. A leading list marker (`-`, `*`) or
  numbering (`1.`, `2)`) is stripped; what remains is the bet's display text.
- A bet names an issue by containing a `#<digits>` token anywhere in its line. A line can name
  more than one issue (`- Reactivate cohort -- #10 #11`); both are treated as this bet.
- Bets that name no issue are still valid -- they are just not build-eligibility hints. A
  `## Bets` section containing only prose (no `#<digits>` anywhere) is a supported, half-written
  plan, not an error: ranking degrades to unchanged order, silently, exit 0.

## What is malformed

Only two things count as malformed, both degrade to unchanged ranking order, and both print
exactly one diagnostic line to stderr (never a traceback):

- No `## Bets` heading anywhere in the file.
- The file exists but can't be read (permissions, bad encoding, a directory where a file was
  expected).

A missing file is NOT malformed -- it is the default, silent, zero-diagnostic case any repo
starts in.

## Optional: `Checkpoints on the number:`

A line starting with `Checkpoints on the number:` anywhere in the file is read verbatim by
`messenger_brief.vision()` for the morning brief's strategy restatement. Optional; no effect on
`plan_rank.py`.

## Full worked example

See `docs/plan/EXAMPLE.md`.
