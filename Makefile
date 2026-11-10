# Local entry point for this independent WSL working copy.
APP_ROOT := $(abspath v3)
UV ?= /home/imini/.local/bin/uv
COMPOSE := docker compose --project-name magr-super-agent --env-file $(APP_ROOT)/.env -f $(APP_ROOT)/deploy/compose.yaml -f $(APP_ROOT)/.local/super-agent/compose.local.yaml
export TMPDIR := $(APP_ROOT)/.local/tmp

.PHONY: help setup deps-up deps-stop migrate doctor lint typecheck test verify
help:
	@echo "magr-super-agent: setup deps-up deps-stop migrate doctor lint typecheck test verify TASK=..."
setup deps-up deps-stop migrate doctor lint typecheck test:
	$(MAKE) -C v3 UV="$(UV)" COMPOSE='$(COMPOSE)' $@
verify:
	$(MAKE) -C v3 UV="$(UV)" COMPOSE='$(COMPOSE)' verify TASK="$(TASK)"
