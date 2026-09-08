# The quality standard: how a request becomes the best version of the thing

Reif, 2026-09-07, after finding the "Telegram-level messenger" issue closed COMPLETED by a
143-line docs PR: *"lots of busy work in terms of PRs, but for some reason the best product
does not get created. Are we optimizing for speed or something? ... Auto close is one thing,
but quality is another thing. Figure out what standards and processes are required in order
to ensure that the highest quality product is put out when a request is made like this."*

This file is the answer. It names the standard practice each rule comes from, so nobody has
to rediscover it, and it says which member enforces it. Written in plain language on purpose:
every member reads it, and so does Reif.

## What went wrong, in one line each

- The person who did part of the work wrote `Fixes #4507`, and GitHub closed the issue.
- The reviewer read the diff, not the issue. It never checked the ask was met.
- "Done" meant "merged". Nobody used the product against the request.
- The hard part (build the feel) never got claimed. The easy part (measure) closed the whole.
- Reif's spec for the person page lived on his laptop. No agent ever saw it.

## 0. The quality dial: the requester says which bar, before anyone builds

Reif, 2026-09-07: *"maybe there is a handle on acceptable criteria. For this project, just
get it out the door. For this project I want literally the user interface and user delight
of Snap or Meta."* The bar is a choice the requester makes, not a guess the builder makes.
Three settings, one label on the issue:

| label | what it means | what the process adds |
|---|---|---|
| `quality:ship-it` | working and safe is enough | Definition of Done only; diff review |
| `quality:solid` | a competent product team would be proud of it | Given/When/Then criteria, a test per criterion, screenshot evidence, plain-language PR |
| `quality:world-class` | the interface and delight of the best in the world at this | everything in solid, plus the research pass below, a design spec Reif approves before build, buy-vs-build spike, design QA against the references, Reif accepts by using it |

Defaults: a request that names a reference product ("Telegram-level", "like Snap", "Stripe
quality") is `world-class`. Any other request from Reif is `solid`. Fleet plumbing is
`ship-it` unless he says otherwise. marie sets the label when she files the PRD; the
messenger's morning brief lists every open `world-class` item so Reif can move the dial.

**The research pass for `world-class`, before a line of code:**

1. Name the two or three best products in the world at this exact thing. Not the category:
   the interaction. For a chat thread: Telegram, iMessage, WhatsApp.
2. Screenshot or record every state of that interaction in each reference: empty, first
   message, sending, sent, delivered, read, typing, offline, reconnect, error, long thread,
   phone width. Put them in `docs/design/<item>/references/`.
3. Write the parity matrix: one row per affordance, one column per reference, one column
   for ours today. Add the budgets people feel (respond within 100 ms, animate at 16 ms a
   frame, load under 1 s).
4. Write the design spec against the references: annotated screenshots or a mock, with the
   states above. Reif approves it (an ask of class `decision`) before anything is built.
5. Find what the best already use: the open-source libraries, UI kits, and named design
   patterns that reproduce each affordance in the matrix (Reif, 2026-09-08: *"find the open
   sourced or design patterns that the best used already"*). Add one column per candidate
   with stars and last release. Then the buy-vs-build spike: an ADR that picks from those
   before any custom build, and names the one reason.
6. Build in vertical slices. After each: design QA against the reference screenshots, side
   by side, at desktop and phone width, in the PR.
7. Reif uses it and says done. Then it closes.

Enforced by: marie's PRD sets the label and, for `world-class`, files the research pass as
slice 1 with the reference list in it; judge-judy blocks a `world-class` PR with no
side-by-side design QA; the closes gate blocks every `Closes` on a `world-class` item until
Reif's acceptance ask is answered. (Label and gates: filed as the next item.)

## The standard, rule by rule

Each rule: what it is, where it comes from, who enforces it here.

### 1. Definition of Done is "the person can do the thing", never "merged"

Scrum's *Definition of Done*: one checklist, the same for every increment, and an item is not
done until every line is true. Ours:

- Every acceptance criterion on the issue is met, and each one has evidence attached to the
  PR: a screenshot or short video for anything a person sees, a named test for anything else.
- The change is live and was used once, by hand, right after it deployed (the verification
  law's third gate: a fix is tested the instant it lands).
- Someone who did not write it says it is done. The author never closes their own ticket.
- For anything Reif asked for in his own words, Reif says it is done.

Enforced by: `closes_gate.py` in judge-judy (a PR that calls itself partial, or is docs-only
on a product item, cannot close an issue), the judge's prompt (it now reads the acceptance
criteria and blocks unless the diff meets every one), and the minion charter (`Part of #N`
plus a "Remaining" list is the only honest way to link partial work).

### 2. Acceptance criteria are testable sentences, and "feel" gets numbers

BDD (Behaviour-Driven Development) writes each criterion as *Given / When / Then* so a test
can be written from it and a reviewer can check it without guessing. ATDD (Acceptance
Test-Driven Development) turns each one into an automated test before the build starts.

For a request like "Telegram-level", the criteria are a **parity matrix** against the named
reference (feature by feature: optimistic send, delivered and read ticks, typing, scroll that
stays put, reconnect banner, unread counts, keyboard, paging) plus **budgets**. Google's RAIL
model gives the numbers people actually feel: respond to input within 100 ms, animate at
16 ms per frame, load in under 1 s. A criterion that says "feels good" is not a criterion.

Enforced by: marie's PRD template requires Given/When/Then and, for `lane:ui`, a parity
matrix with budgets. gru does not claim an item whose criteria are not testable sentences.

### 3. Every slice is something a person can use

Stories follow INVEST (independent, negotiable, valuable, estimable, small, testable). A
slice is *vertical*: it changes what the person can do, end to end. "Measure the gap" is a
slice for the planner, not a slice of the product, and it can never close the parent.

An epic closes only when every child is accepted. GitHub sub-issues carry that structure.

Enforced by: marie decomposes into vertical slices and links them as sub-issues; the closes
gate refuses a `Closes` from a docs-only PR on a product item; jefe closes epics, not PRs.

### 4. The reviewer reads the ticket, and asks for proof per criterion

A code review checks that the code is correct. An **acceptance review** checks that the
request is met. They are different jobs, and the second one was missing. The PR template
carries the criteria as checkboxes, each with its evidence link, and the reviewer checks the
evidence, not the checkbox.

Enforced by: judge-judy's prompt carries the issue's acceptance criteria; a criterion with no
evidence in the PR is a block. The PR body template (persona law §10d) gains an
"Acceptance criteria" block with evidence per line.

### 5. The requester accepts, by using it

*User Acceptance Testing* and the sprint review: the person who asked looks at the working
thing and says yes or no. Nothing else counts as acceptance for a request in Reif's words.

Enforced by: issues Reif filed or quoted get the label `fleet:reif-asked`. The closes gate
blocks every `Closes` on them. When all criteria have evidence, the finishing PR files an ask
of class `acceptance` with the demo link; Reif answers from the brief; jefe closes the issue
with the answer quoted.

### 6. Quality gates in CI, not in opinions

Automated checks that fail the build: end-to-end tests for each acceptance criterion
(Playwright), a performance budget (Lighthouse CI against the RAIL numbers), accessibility
(axe), and visual regression for UI slices. A green check is the floor; rule 1 is the bar.

Enforced by: the product repo's CI. Filed as its own item there; not fleet-kit's to build.

### 7. Buy before build, with a written decision

When the request names a reference product ("Telegram-level", "Stripe-quality checkout"),
the first slice is a spike: try two existing libraries or products against the parity matrix,
and write an ADR (Architecture Decision Record) saying which one, or why hand-rolled wins.
Hand-rolling 2,500 lines of chat client across three copies is the outcome of skipping this.

Enforced by: marie's PRD for any "X-level" request starts with the spike as slice 1.

### 8. Outcome over output

The item names the number it should move (Vision-link, already law) and the finishing PR
names how to see it move. A week later the learner reads whether it did. Shipping is not
the goal; the number moving is.

Enforced by: dumbledore's weekly plan reads each accepted item's number delta.

### 9. Freshman 101 language, everywhere, always

Reif, 2026-09-07: *"every time a member writes anything, ever, it should be done in freshman
101 language."* Reports, PR bodies, issue comments, emails, briefs, commit messages. Short
sentences. Common words. Say the result first. No acronym without its plain meaning the
first time. Numbers in a small table, not in a sentence. If a smart person outside software
could not follow it, rewrite it.

Enforced by: persona law §13; judge-judy blocks a PR body a lay reader cannot follow.

## What is built today, and what is filed

Built in this PR: closes gate (rules 1, 3), judge reads acceptance criteria (rule 4), minion
`Part of` rule (rule 1), persona law §13 plain language (rule 9) and §14 Definition of Done.

Filed as the next items: marie's Given/When/Then + parity-matrix template and the spike-first
rule for "X-level" asks (rules 2, 7); vertical-slice decomposition with sub-issues and
jefe-closes-epics (rule 3); `fleet:reif-asked` plus the acceptance ask (rule 5); product-repo
CI gates (rule 6); the learner's outcome read-back (rule 8).
