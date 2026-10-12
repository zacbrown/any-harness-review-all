# Makefile for /review-all skill
#
# `make install`     — copy skills/review-all/ into ~/.claude/skills/review-all/
# `make uninstall`   — remove the installed copy
# `make review-self` — install, then remind to run /review-all in this repo

SKILL_SRC := skills/review-all
SKILL_DEST := $(HOME)/.claude/skills/review-all
PROJECT ?= .

.PHONY: install uninstall review-self help install-copilot install-copilot-project uninstall-copilot uninstall-copilot-project

help:
	@echo "Targets:"
	@echo "  install     copy $(SKILL_SRC)/ to $(SKILL_DEST)/"
	@echo "  uninstall   delete $(SKILL_DEST)/"
	@echo "  review-self install, then run /review-all on this repo"
	@echo "  install-copilot / uninstall-copilot   user Copilot skill and agents"
	@echo "  install-copilot-project / uninstall-copilot-project   PROJECT=/path/to/repo"

install:
	@mkdir -p "$(SKILL_DEST)"
	@rsync -a --delete "$(SKILL_SRC)/" "$(SKILL_DEST)/"
	@echo "Installed: $(SKILL_DEST)"

uninstall:
	@rm -rf "$(SKILL_DEST)"
	@echo "Removed: $(SKILL_DEST)"

review-self: install
	@echo "Now run /review-all inside Claude Code in this repo."

install-copilot:
	python3 "$(SKILL_SRC)/scripts/copilot-install.py" install --user

uninstall-copilot:
	python3 "$(SKILL_SRC)/scripts/copilot-install.py" uninstall --user

install-copilot-project:
	python3 "$(SKILL_SRC)/scripts/copilot-install.py" install --project "$(PROJECT)"

uninstall-copilot-project:
	python3 "$(SKILL_SRC)/scripts/copilot-install.py" uninstall --project "$(PROJECT)"
