.PHONY: install sync dry-run test bootstrap-spotify bootstrap-qq

install:
	pip install -r requirements.txt

sync:
	python -m src.main sync

dry-run:
	python -m src.main sync --dry-run

test:
	python -m pytest tests/ -q

bootstrap-spotify:
	python -m src.main bootstrap-spotify

bootstrap-qq:
	python -m src.main bootstrap-qq
