# Run the product delivery check

Run `make verify-product` from a clean checkout. The command starts the
Temporal worker and runs the fixture with no GitHub credentials. It submits the
same run twice, drops the PR creation response, and recovers one PR receipt. A
second run reaches its deadline with approval withheld. The command removes the
worker and reopens its evidence in a read-only Compose workspace.

Find the archive under `${EVIDENCE_ROOT:-.evidence}/verify-product.*`. The
`tests.log` also covers a changed head, approvals for another scope or past
their expiry, and repeated fixture deployments. Deployment uses the repository,
revision, and environment as its key.

## Deliver the retained P03 submission

Run the host commands from the factory checkout. The host needs authenticated
Git and `gh`, Python 3.12, and Docker Compose. Keep the evidence directory
outside disposable checkouts and accessible to the trusted runner. The product
workspace runs with no network and receives the product checkout read-only
during the CI suite.

1. Confirm the target, required check and publisher, independent reviewer,
   observation window, and each allowed write with the maintainer.
2. Set `PYTHONPATH=src` on the host. Use the following commands with your
   evidence directory, P03 archive, and records fixture archive. Set the
   deadline and expiry to UTC epoch seconds.

   ```sh
   python3.12 -m factory.product_cli --evidence "$ARCHIVE" "$RUN" admit \
     --source "$P03_ARCHIVE" --review-source frozen_grader --reviewer frozen-p03-grader \
     --check p04/verified-patch-suite --check-actor "$PUBLISHER" \
     --deadline "$DEADLINE" --observation-seconds 900
   ```

3. Record each approved action with the maintainer's login, scope expiry, and
   source of authorization. Run `approve` once for each of `push`,
   `pull_request`, and `ci_status` that the maintainer authorized.

   ```sh
   python3.12 -m factory.product_cli --evidence "$ARCHIVE" "$RUN" approve "$ACTION" \
     --actor "$MAINTAINER" --expires-at "$EXPIRY" --source "$AUTHORIZATION"
   ```

4. Start and follow the delivery.

   ```sh
   python3.12 -m factory.product_cli --evidence "$ARCHIVE" "$RUN" run --fixture "$RECORDS_ARCHIVE"
   ```

   The runner checks the retained P03 bundle, checkpoint, recovered verdict, and
   maintainer acceptance. It runs the patch suite in Compose and reruns the
   independent grader using its retained digest. Use `--review-source github`
   and the reviewer login when the contract calls for a GitHub review.

5. Use `observe` with `--actor`, `--pull-request`, and `--feedback` to add a
   maintainer observation during the window. Include `--active-human-minutes`
   when measured. The report uses `unknown` for unavailable measurements.
6. Run `reopen` with the same archive and run ID after checkout cleanup.
   The report binds the retained blobs, approvals, receipts, and observation
   window to the exact head and the Temporal execution.

## Integrate a new head

Use a new run ID for a checked-out integration commit that descends from the
original, observed product revision. Run `prepare-integration` with
`--prior-run`, `--checkout`, `--base-sha`, `--revision`, and `--fixture`.
This command runs the frozen grader on the new revision.

Once the maintainer authorizes the new SHA, run `admit-integration` with
`--prior-run`, `--revision`, and `--deadline`. Record fresh grants for
`push` and `ci_status`, then run the new delivery. The branch update uses a Git
lease on the old SHA. The runner verifies the existing PR number, base, and run
marker before opening a new observation window on the new SHA.

If GitHub loses a write response, the runner reconciles the expected ref,
PR, or status. When the lookup conflicts or gives no matching result after an
attempt, the run records the conflict for the operator. A head change during an
observation closes that run unshipped. The GitHub connector supports branch
push, PR creation, and CI status publication. The test deployment stays in
the fixture.
