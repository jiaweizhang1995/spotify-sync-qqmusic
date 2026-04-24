.PHONY: install lock sync dry-run full test bootstrap-spotify bootstrap-qq

install:
	uv sync

lock:
	uv lock

sync:
	uv run spotify-sync sync

dry-run:
	uv run spotify-sync sync --dry-run

full:
	uv run spotify-sync sync --full

test:
	uv run pytest tests/ -q

bootstrap-spotify:
	uv run spotify-sync bootstrap-spotify

bootstrap-qq:
	uv run spotify-sync bootstrap-qq
