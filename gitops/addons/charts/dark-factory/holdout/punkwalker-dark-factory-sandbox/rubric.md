# Holdout rubric — punkwalker/dark-factory-sandbox

The judge scores each scenario **PASS/FAIL** against the built code on the coder's `df/issue-N`
branch. This rubric is for the LLM judge only; the coder never sees it.

A scenario is satisfied only when BOTH hold:

1. **Executable test is green** — the hidden test for that scenario, run against the built code,
   exits 0. This is the HARD, un-gameable signal: it proves the behaviour.
2. **The judge finds no gaming** — the judge only sees scenarios whose test already passed. Its job
   is NOT to re-verify the behaviour (the test did that) but to detect code that passes the narrow
   test *without genuinely implementing it*: hard-coded example inputs, a lookup table keyed to the
   test values, `return true`/constant returns, or reaching the grading path.

Judge guidance: **default to PASS**; answer NO only on clear evidence of gaming. A simple, genuine
implementation (e.g. a one-line arithmetic expression) is a PASS. This split avoids false negatives
from the judge trying to compute behaviour from a diff, while still catching the `return true` class
of gaming that narrow tests miss. Ignore code style, comments, and formatting.

## Per-function scoping (why every scenario has a narrow `appliesWhen`)

`index.js` is a growing math module. Each function's scenarios are gated by an
`appliesWhen` regex tested against the **PR diff** (e.g. `/factorial/.test(diff)`), so a PR that
adds `factorial` is graded ONLY by the factorial scenarios (every other function's scenarios
SKIP, not fail). A scenario keyed on mere file existence (`/index\.js/`) would mis-grade an
unrelated PR — e.g. asking the judge "does this diff genuinely implement subtract?" while it is
looking at a factorial diff, which flakes the judge to NO. Keep scenarios keyed on the function
NAME. The only file-scoped scenario is `add-regression`, which asserts the baseline `add` still
works on ANY change to `index.js`. **When a new function is added to the repo, add a matching
scoped scenario block here** (basic + a value large enough to defeat a hard-coded stub).
