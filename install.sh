#!/usr/bin/env bash
set -euo pipefail

# Harness Installer — copies harness into any project as .harness/
#
# Usage:
#   bash ~/harness/install.sh                    # install into current directory
#   bash ~/harness/install.sh /path/to/project   # install into target project

HARNESS_ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-.}"
TARGET="$(cd "$TARGET" && pwd)"
DEST="$TARGET/.harness"

if [ -d "$DEST" ]; then
  echo "⚠  .harness/ already exists at $TARGET"
  echo "   Use --force to overwrite, or delete it first."
  if [ "${2:-}" != "--force" ]; then
    exit 1
  fi
  echo "   --force: updating code, preserving state..."
  # NEVER delete state — it contains run progress, work plans, fleet sessions
  if [ -d "$DEST/state" ]; then
    mv "$DEST/state" "/tmp/.harness-state-backup-$$"
  fi
  if [ -d "$DEST/../.harness-fleet" ] || [ -d "$DEST/fleet" ]; then
    mv "$DEST/fleet" "/tmp/.harness-fleet-backup-$$" 2>/dev/null || true
  fi
  rm -rf "$DEST"
fi

echo "Installing harness into $TARGET/.harness/"

# Copy core files
mkdir -p "$DEST"
cp -r "$HARNESS_ROOT/src/core" "$DEST/core"
cp -r "$HARNESS_ROOT/src/prompts" "$DEST/prompts"
cp -r "$HARNESS_ROOT/src/templates" "$DEST/templates"
cp -r "$HARNESS_ROOT/src/hooks" "$DEST/hooks"
cp "$HARNESS_ROOT/src/run.py" "$DEST/run.py"
cp "$HARNESS_ROOT/src/config.yaml" "$DEST/config.yaml"
cp "$HARNESS_ROOT/src/requirements.txt" "$DEST/requirements.txt"
cp "$HARNESS_ROOT/src/__init__.py" "$DEST/__init__.py"

# Restore preserved state (if --force upgrade)
if [ -d "/tmp/.harness-state-backup-$$" ]; then
  mv "/tmp/.harness-state-backup-$$" "$DEST/state"
  echo "  State preserved (work plans, run progress intact)"
else
  mkdir -p "$DEST/state"
fi
if [ -d "/tmp/.harness-fleet-backup-$$" ]; then
  mkdir -p "$DEST/fleet"
  mv /tmp/.harness-fleet-backup-$$/* "$DEST/fleet/" 2>/dev/null || true
  rm -rf "/tmp/.harness-fleet-backup-$$"
fi

# Install Claude Code skills
SKILLS_DIR="$TARGET/.claude/skills"
if [ -d "$HARNESS_ROOT/src/skills" ]; then
  mkdir -p "$SKILLS_DIR"
  for skill in "$HARNESS_ROOT/src/skills"/*/; do
    skill_name=$(basename "$skill")
    mkdir -p "$SKILLS_DIR/$skill_name"
    cp "$skill/SKILL.md" "$SKILLS_DIR/$skill_name/SKILL.md"
  done
  echo "  Skills installed: $(ls "$HARNESS_ROOT/src/skills" | tr '\n' ', ' | sed 's/,$//')"
fi

# Update .gitignore — add harness runtime artifacts (idempotent)
GITIGNORE="$TARGET/.gitignore"
ENTRIES=(".harness/state/" ".harness/core/__pycache__/" ".harness/**/__pycache__/" ".harness/**/*.pyc")
if [ -f "$GITIGNORE" ]; then
  for entry in "${ENTRIES[@]}"; do
    grep -qxF "$entry" "$GITIGNORE" || echo "$entry" >> "$GITIGNORE"
  done
else
  printf '%s\n' "${ENTRIES[@]}" > "$GITIGNORE"
fi
echo "  .gitignore updated"

# Install Python dependencies
if command -v pip3 &>/dev/null; then
  pip3 install -q -r "$DEST/requirements.txt" 2>/dev/null || true
elif command -v pip &>/dev/null; then
  pip install -q -r "$DEST/requirements.txt" 2>/dev/null || true
fi

echo ""
echo "✓ Harness installed at $DEST"
echo ""
echo "Usage:"
echo "  # Autonomous build from prompt"
echo "  cd $TARGET"
echo "  python3 .harness/run.py --prompt 'Build a todo app with auth'"
echo ""
echo "  # Build from spec"
echo "  python3 .harness/run.py --spec docs/specs/my-feature.md"
echo ""
echo "  # Build from plan"
echo "  python3 .harness/run.py --plan docs/plans/my-plan.md"
echo ""
echo "  # Resume after crash"
echo "  python3 .harness/run.py --resume"
echo ""
echo "  # With limits"
echo "  python3 .harness/run.py --prompt '...' --max-cost 50 --max-iterations 20"
echo ""
echo "  # Enable parallel fleet mode"
echo "  Edit .harness/config.yaml → parallel.enabled: true"
echo ""
echo "Config: $DEST/config.yaml"
echo "Prompts: $DEST/prompts/"
