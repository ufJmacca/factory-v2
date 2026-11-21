# Hold the Phase 05 delivery skill

**Hold for missing live evidence.** The scripted comparison scored 4 of 18
planned assignments on their submitted SHAs. Four assignments were accepted.
Six had a Harbor attempt. Two attempts returned deliberately invalid grade
evidence, and the other 12 assignments stopped at the scripted cap. There
were zero subscription executions and four fixture effects.

| Split | Planned | Attempts | Scored on the submitted SHA | Accepted |
| --- | ---: | ---: | ---: | ---: |
| Historical `verified-patch` | 6 | 2 | 1 | 1 |
| Held-out `rpc-unknown-operation` and `profile-preview` | 12 | 4 | 3 | 3 |

Both held-out known revisions passed calibration. Both unchanged bases and
both regression controls failed their frozen contracts. The comparison
discarded the first submission reply and a new supervisor recovered the
original Harbor job. Successful assignments completed with the grade
notification suppressed. A malformed historical grade and a changed
held-out grader digest produced zero fixture effects on those attempts.
The zero-acceptance accounting test leaves cost per accepted outcome
undefined. The run records unknown subscription and infrastructure cost.

## Follow the source outcome

1. Open the source pointers for `pr03-proxy-final`. At native events 19
   and 21, the workspace bridge rejected an extra argument and then ran
   the corrected call. The archived model context remains unknown.
   [Phase 05 evidence](../p05/README.md)
2. Review the hunk that requires a single `command` field. The candidate
   configuration resolves only that changed skill. Both arms use the
   same worker image, selector, and CLI build. The newer worker and
   historical Harbor grader wrapper are pinned in the experiment record.
   [Candidate patch](../p05/candidate.patch) · [Frozen experiment](frozen-experiment.json)
3. Open historical assignments `p08-01` and `p08-02`. The scripted fixture
   produced one initial extra-key rejection in the baseline. The candidate
   grade was deliberately invalid. The historical prediction is inconclusive.
   The held-out split has one complete scripted pair and no observed
   acceptance regression in that pair.
   [Assignment ledger](assignment-ledger.json) · [Paired results](paired-results.json)

Fresh approval is pending for the live envelope. The scripted run exercises
the comparison and grading path. Live behavior, live regressions, and
attributable live cost remain unverified. Repeats on two independent held-out
tasks also leave generality uncertain. This decision covers the pilot.
Phase 09 owns activation and rollback.
