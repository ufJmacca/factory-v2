.PHONY: test

test:
	docker compose run --build --rm --no-deps -T workspace uv run --locked --offline python -m unittest discover -s tests -v
