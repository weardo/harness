#!/usr/bin/env bash
set -euo pipefail

# Harness Installer — copies harness into any project as .harness/
#
# Usage:
#   bash ~/harness/install.sh                          # install into current dir
#   bash ~/harness/install.sh /path/to/project         # install into target project
#   bash ~/harness/install.sh /path/to/project --force # update code, preserve state
#   bash ~/harness/install.sh --force                  # update in current dir
#   bash ~/harness/install.sh --force /path/to/project # also works
#
# --force: Updates harness code while preserving:
#   - runs/ (all run history, work plans, fleet sessions, per-run knowledge)
#   - runs.json (run registry)
#   - config.yaml (user customizations)
#   - knowledge/ (persistent cross-run learnings)
#
# Knowledge directories:
#   .harness/knowledge/     — persistent learnings across runs (curated)
#   .harness/runs/<run>/knowledge/ — per-run task briefs (auto-generated)

HARNESS_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Parse args: support both "path --force" and "--force path" order
FORCE=false
TARGET="."
for arg in "$@"; do
  if [ "$arg" = "--force" ]; then
    FORCE=true
  else
    TARGET="$arg"
  fi
done
TARGET="$(cd "$TARGET" && pwd)"
DEST="$TARGET/.harness"

if [ -d "$DEST" ]; then
  echo "⚠  .harness/ already exists at $TARGET"
  echo "   Use --force to overwrite, or delete it first."
  if [ "$FORCE" != "true" ]; then
    exit 1
  fi
  echo "   --force: updating code in-place (state, runs, knowledge untouched)..."
fi

echo "Installing harness into $TARGET/.harness/"
mkdir -p "$DEST"

# Selective install: only overwrite CODE directories, never touch state.
# Safe to run while agents are reading from runs/, knowledge/, etc.
#
# Uses atomic swap (cp to .new → mv old → mv new) so agents never see
# a missing directory mid-install. Single files are overwritten in-place
# which is atomic on macOS/Linux for same-filesystem writes.
for dir in core prompts templates hooks; do
  cp -r "$HARNESS_ROOT/src/$dir" "$DEST/$dir.new"
  [ -d "$DEST/$dir" ] && mv "$DEST/$dir" "$DEST/$dir.old"
  mv "$DEST/$dir.new" "$DEST/$dir"
  rm -rf "$DEST/$dir.old"
done
cp "$HARNESS_ROOT/src/run.py" "$DEST/run.py"
cp "$HARNESS_ROOT/src/requirements.txt" "$DEST/requirements.txt"
cp "$HARNESS_ROOT/src/__init__.py" "$DEST/__init__.py"

# config.yaml: only copy if it doesn't exist (fresh install)
if [ ! -f "$DEST/config.yaml" ]; then
  cp "$HARNESS_ROOT/src/config.yaml" "$DEST/config.yaml"
else
  echo "  config.yaml preserved (user customizations kept)"
fi

# These are NEVER touched — they persist across installs:
#   runs/       — run history, work plans, fleet sessions, per-run knowledge
#   runs.json   — run registry
#   knowledge/  — persistent cross-run learnings
#   state/      — legacy state (auto-migrates on first run)
# No state/ dir created on fresh install — _setup_run() creates runs/ on demand

# Install Claude Code skills (SKILL.md + any scripts like status.py)
SKILLS_DIR="$TARGET/.claude/skills"
if [ -d "$HARNESS_ROOT/src/skills" ]; then
  mkdir -p "$SKILLS_DIR"
  for skill in "$HARNESS_ROOT/src/skills"/*/; do
    skill_name=$(basename "$skill")
    mkdir -p "$SKILLS_DIR/$skill_name"
    # Copy all files in the skill directory (SKILL.md, *.py, etc.)
    cp "$skill"* "$SKILLS_DIR/$skill_name/" 2>/dev/null || true
  done
  echo "  Skills installed: $(ls "$HARNESS_ROOT/src/skills" | tr '\n' ', ' | sed 's/,$//')"
fi

# Update .gitignore — add harness runtime artifacts (idempotent)
GITIGNORE="$TARGET/.gitignore"
ENTRIES=(".harness/state/" ".harness/runs/" ".harness/runs.json" ".harness/core/__pycache__/" ".harness/**/__pycache__/" ".harness/**/*.pyc")
# NOTE: .harness/knowledge/ is NOT gitignored — it's committed so persistent
# learnings are shared across machines and team members.
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
