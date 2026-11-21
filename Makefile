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
