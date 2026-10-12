skill_src := "skills/review-all"
skill_dest := home_directory() / ".claude/skills/review-all"

# List available recipes (also the default action).
help:
    @just --list

# Install the Claude Code skill.
install:
    @mkdir -p "{{ skill_dest }}"
    @rsync -a --delete "{{ skill_src }}/" "{{ skill_dest }}/"
    @echo "Installed: {{ skill_dest }}"

# Remove the installed Claude Code skill.
uninstall:
    @rm -rf "{{ skill_dest }}"
    @echo "Removed: {{ skill_dest }}"

# Install, then show how to review this repository.
review-self: install
    @echo "Now run /review-all inside Claude Code in this repo."

# Install the user-level Copilot skill and agents.
install-copilot:
    python3 "{{ skill_src }}/scripts/copilot-install.py" install --user

# Remove unchanged package-owned user-level Copilot files.
uninstall-copilot:
    python3 "{{ skill_src }}/scripts/copilot-install.py" uninstall --user

# Install the Copilot skill and agents into a project (default: this repository).
install-copilot-project project=".":
    python3 "{{ skill_src }}/scripts/copilot-install.py" install --project "{{ project }}"

# Remove unchanged package-owned Copilot files from a project.
uninstall-copilot-project project=".":
    python3 "{{ skill_src }}/scripts/copilot-install.py" uninstall --project "{{ project }}"
