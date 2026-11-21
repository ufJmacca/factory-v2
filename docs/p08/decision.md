# Hold the Phase 05 delivery skill

**Hold for incomplete held-out grading.** The live batch reconciled all 18
planned assignments and all 18 attempts. Protected graders accepted 15
submissions on their exact SHAs. Three `profile-preview` attempts retained
submitted SHAs and traces but lacked the required workspace marker. They
stopped before grading. The four complete held-out pairs had equal
acceptance, with both arms passing in each pair.

| Split | Task | Baseline accepted / planned | Candidate accepted / planned | Exact-SHA grades |
| --- | --- | ---: | ---: | ---: |
| Historical | `verified-patch` | 3 / 3 | 3 / 3 | 6 / 6 |
| Held-out | `rpc-unknown-operation` | 3 / 3 | 3 / 3 | 6 / 6 |
| Held-out | `profile-preview` | 1 / 3 | 2 / 3 | 3 / 6 |
| Total | | 7 / 9 | 8 / 9 | 15 / 18 |

The historical result supported the frozen prediction. Across three complete
pairs, the candidate recorded 9 initial extra-argument rejections and the
baseline recorded 11. All six historical submissions passed. Two held-out
pairs lack complete grading, so the paired acceptance gain is unknown.
The decision applies to this pilot. Phase 09 owns activation and rollback.
[Live paired results](live-paired-results.json)

## Grade coverage and resource use

The terminal records for `p08-05`, `p08-06`, and `p08-11` report
`workspace_marker_missing`. Their traces contain completed workspace calls
and no completed workspace call referencing `.git/factory-marker`. The
retained evidence establishes the missing isolation proof. The three
submitted revisions remain ungraded. [Live assignment ledger](live-assignment-ledger.json)

The maintainer approved the frozen 18-execution envelope in Codex chat.
The run used 18 of 18 subscription executions, one at a time, with zero
retries and zero API spend. It took 63 minutes and 33 seconds against the
four-hour batch cap. Harbor recorded 18 jobs, 18 agent containers, and 15
protected grades. The six offline calibration trials accepted each known
revision and rejected each unchanged base and regression control.

A read-only audit reopened the 18 Temporal terminal manifests and matched
the job, rollout, submitted SHA, grader digest, and retained evidence where
grading completed. The fixture ledger held 15 effects, one per accepted
assignment. The evaluation recorded zero production effects. Docker
removed the trial containers and Compose project services.

All 18 executions reported token usage. Subscription cost and attributable
infrastructure cost are unknown. Cost per accepted outcome is unknown.
[Frozen live experiment](live-frozen-experiment.json) · [Live paired results](live-paired-results.json)

## Follow the source outcome

1. Open the source pointers for `pr03-proxy-final`. Native events 19 and 21
   show the workspace bridge rejecting an extra argument and running the
   corrected call. The archived model context remains unknown.
   [Phase 05 evidence](../p05/README.md)
2. Review the hunk that requires a single `command` field. The candidate
   configuration resolves only that changed skill. Both arms use the same
   worker image, selector, and CLI build. The current worker and the
   historical Harbor grader wrapper are pinned in the live manifest.
   [Candidate patch](../p05/candidate.patch) · [Frozen live experiment](live-frozen-experiment.json)
3. Open `p08-01` and `p08-02` for the first live historical pair. Both
   submissions passed their frozen grader on their exact SHAs. The ledger
   binds each outcome to its job, rollout, Temporal run, native trace,
   usage, grade, and fixture receipt. The paired results show all three
   historical pairs and the two incomplete held-out pairs.
   [Live assignment ledger](live-assignment-ledger.json) · [Live paired results](live-paired-results.json)

## Scripted verification before approval

The scripted batch discarded its first Harbor submit reply and recovered
the original job in a new supervisor. It reconciled accepted assignments
with the grade notification suppressed. A malformed grade and a changed
grader digest produced zero fixture effects. The active-stop drill
cancelled the running attempt, reconciled all 18 assignments, and left
cost per accepted outcome undefined. The scripted and live evidence
records each name the checkout that produced them.
[Scripted experiment](frozen-experiment.json) · [Scripted assignment ledger](assignment-ledger.json) · [Scripted paired results](paired-results.json)
