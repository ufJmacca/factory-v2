# Phase 09 promotion and rollback

The revised candidate adds the direct-launch rule to the milestone argument
description. The sealed comparison at
`57889c7ad4ecee364e52841e497cfd6180035c52` accepted all 18 exact submissions.
The first red launch improved in five of eight held-out pairs. Three pairs
tied. The pilot used 18 sequential subscription executions with zero retries
and zero API spend.

The maintainer approved promotion for that tested revision. The experiment
digest is
`238d5e5536401bbccae22bac66dfdb9bc859d4c73bf456336b870ee8087485cd`.
The decision evidence digest is
`5643f4f39c9eadff27d3a5fb6d510442323ccb774e8d1f9199617e7e667cf029`.
The promotion decision digest and both version digests appear in the
[fixture evidence](activation-rollback-fixture.json).

## Fixture results

The baseline admission retained its digest after activation. The next eligible
admission used the candidate digest. After rollback, a new admission used the
restored baseline digest. The fixture called the MCP bridge in the exact pilot
worker image for all three persisted requests. The returned schemas matched the
original baseline, candidate, and baseline pins.

Each transition advanced the generation by one. Duplicate requests returned the
original receipts. The fixture recorded a rollback decision when the reopen
limit was crossed. The [transition tests](../../tests/test_promotion.py) also
cover expired approvals, competing promotions, the cohort cap and cutoff, and
unknown costs. The [input tests](../../tests/test_promotion_inputs.py) cover
drift in the bot selector, skill files, helpers, and worker image. Run
`make test` from a clean checkout to repeat the offline tests.

## Activation gate

Use the trusted Compose workspace for both promotion modules. The
`record-review` command in `scripts.promotion_inputs` imports the named
reviewer's decision and checks the pilot evidence against its sealed digests.
Use the host `gh` and Git to resolve the merge commit and fetch `origin/main`.
Supply the deployed worker image ID from host Docker to `verify-merged`.
It issues parity evidence when the merged commit is on the fetched main branch
and its effective inputs match the reviewed candidate. Input drift requires
fresh approval for affected trials.

An activation request to `scripts.promotion` binds the prior digest and
generation, reviewed candidate, promotion decision, merged parity evidence, and
a separate operator authorization with an expiry. Its scope fixes the outcome
class, cohort, run cap, observation window, and limits for reopens, rollbacks,
reviewer seconds, and attributable cost in millionths of a US dollar. The
transition receipt records the baseline as the rollback target. An approved
rollback checks the current digest and generation and restores that baseline.

Admit each approved cohort outcome through `scripts.promotion admit` before
submitting the persisted request to the delivery worker. Report observations
as increments with stable IDs. The registry records a rollback decision when a
known total crosses its limit. Missing measurements keep the report incomplete.

The fixture used the class `offline-python-fix`, with a two-run cap and a
50-second observation window. Live cohort parameters and live activation are
pending separate authorization. The baseline is active.
