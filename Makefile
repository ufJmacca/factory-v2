.PHONY: test verify-fixtures

test:
	docker compose run --build --rm --no-deps -T workspace uv run --locked --offline python -m unittest discover -s tests -v

verify-fixtures:
	./scripts/verify-fixtures.sh

.PHONY: verify-recovery

verify-recovery:
	./scripts/verify-recovery.sh

.PHONY: verify-codex

verify-codex:
	./scripts/verify-codex.sh

.PHONY: verify-product

verify-product:
	./scripts/verify-product.sh

.PHONY: verify-proposal

verify-proposal:
	./scripts/verify-proposal.sh

.PHONY: verify-harbor

verify-harbor:
	./scripts/verify-harbor.sh

.PHONY: verify-harbor-bridge

verify-harbor-bridge:
	./scripts/verify-harbor-bridge.sh

.PHONY: verify-harbor-experiment

verify-harbor-experiment:
	./scripts/verify-harbor-experiment.sh
