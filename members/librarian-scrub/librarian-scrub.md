---
name: librarian-scrub
description: >
  Shell member (fleet-kit#784): the hourly credential scrub + retention sweep, run by
  librarian-scrub.sh -> members/librarian/librarian.py --execute. No model. This file is
  kept for the roster/console; run_member.sh never loads it (llm.runner bypasses prompt_file)
  and llm.tools denies everything. The model-shaped librarian jobs (memory, intent) run once a
  day as `librarian`.
model: sonnet
tools: none
---

Not a prompt. See librarian-scrub.sh and librarian.py's own docstring.
