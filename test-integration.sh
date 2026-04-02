#!/usr/bin/env bash
# Integration tests for the long-running agent harness.
# Run in a STANDALONE terminal — no active Claude Code session.
#
# Usage: bash test-integration.sh [test-name]
#   test-name: counter | todo | hook | parallel | all (default: all)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_DIR="$SCRIPT_DIR/test-projects"
SRC_DIR="$SCRIPT_DIR/src"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}PASS${NC}: $1"; }
fail() { echo -e "${RED}FAIL${NC}: $1"; exit 1; }
info() { echo -e "${YELLOW}INFO${NC}: $1"; }

install_harness() {
    local project_dir="$1"
    mkdir -p "$project_dir/.harness"
    cp -r "$SRC_DIR/"* "$project_dir/.harness/"
    mkdir -p "$project_dir/.harness/state"
    info "Harness installed in $project_dir"
}

# ─── Test 1: Counter App (--prompt mode) ───
test_counter() {
    info "Test 1: Counter App (--prompt mode)"
    local project="$TEST_DIR/counter-app-test"
    rm -rf "$project"
    mkdir -p "$project"
    cd "$project"
    git init && git config user.email "test@test.com" && git config user.name "Test"
    echo "# Counter App" > README.md && git add . && git commit -m "init"

    install_harness "$project"

    python3 .harness/run.py \
        --prompt "Build a simple HTML page with a counter. Has an increment button, decrement button, and a display showing the count. Use vanilla HTML/CSS/JS only." \
        --max-iterations 3 \
        --max-cost 10 \
        --project-dir .

    # Verify outputs
    [ -f .harness/state/state.json ] && pass "state.json created" || fail "state.json missing"
    [ -f .harness/state/feature_list.json ] && pass "feature_list.json created" || fail "feature_list.json missing"

    local passing=$(python3 -c "import json; d=json.load(open('.harness/state/feature_list.json')); print(sum(1 for f in d if f.get('passes')))")
    info "Features passing: $passing"

    [ "$(git log --oneline | wc -l)" -gt 1 ] && pass "Git commits made" || fail "No git commits"

    info "Test 1 complete"
}

# ─── Test 2: Todo App (--spec mode) ───
test_todo() {
    info "Test 2: Todo App (--spec mode)"
    local project="$TEST_DIR/todo-app-test"
    rm -rf "$project"
    mkdir -p "$project"
    cd "$project"
    git init && git config user.email "test@test.com" && git config user.name "Test"
    echo "# Todo App" > README.md && git add . && git commit -m "init"

    install_harness "$project"

    # Create a simple spec
    cat > spec.md << 'SPEC'
# Todo App Specification

A simple todo list application using vanilla HTML/CSS/JS.

## Features
1. Add new todo items via text input + button
2. Display list of todos
3. Mark todos as complete (strikethrough)
4. Delete individual todos
5. Show count of remaining todos

## Tech Stack
- HTML5
- CSS3 (flexbox layout)
- Vanilla JavaScript (no frameworks)
- localStorage for persistence
SPEC

    python3 .harness/run.py \
        --spec spec.md \
        --max-iterations 3 \
        --max-cost 10 \
        --project-dir .

    [ -f .harness/state/feature_list.json ] && pass "feature_list.json created from spec" || fail "feature_list.json missing"
    [ -f .harness/state/spec.md ] || info "Note: spec.md in state/ not created (planner may write to project root)"

    info "Test 2 complete"
}

# ─── Test 3: Stop Hook (Mode B) ───
test_hook() {
    info "Test 3: Stop Hook (Mode B)"
    local project="$TEST_DIR/hook-test"
    rm -rf "$project"
    mkdir -p "$project/.harness/state"
    cd "$project"

    install_harness "$project"

    # Create a feature list with incomplete features
    cat > .harness/state/feature_list.json << 'FL'
[
  {"id": "001", "passes": false, "blocked": false, "description": "Test feature 1"},
  {"id": "002", "passes": true, "blocked": false, "description": "Test feature 2"}
]
FL

    # Test hook — should block (features remaining)
    echo '{"stop_hook_active": false}' | python3 .harness/hooks/stop_completion.py > /tmp/hook_output.json 2>&1
    if grep -q "block" /tmp/hook_output.json 2>/dev/null; then
        pass "Hook blocks when features incomplete"
    else
        fail "Hook should block when features incomplete"
    fi

    # Mark all passing
    cat > .harness/state/feature_list.json << 'FL'
[
  {"id": "001", "passes": true, "blocked": false, "description": "Test feature 1"},
  {"id": "002", "passes": true, "blocked": false, "description": "Test feature 2"}
]
FL

    # Add completion signals
    python3 -c "
import json
from pathlib import Path
signals = {'completion_signals': [1, 2], 'blocked_signals': [], 'error_signals': []}
Path('.harness/state/exit_signals.json').write_text(json.dumps(signals))
"

    # Test hook — should allow (all complete + signals)
    output=$(echo '{"stop_hook_active": false}' | python3 .harness/hooks/stop_completion.py 2>&1)
    if [ -z "$output" ] || ! echo "$output" | grep -q "block"; then
        pass "Hook allows exit when all features complete"
    else
        fail "Hook should allow exit when complete"
    fi

    # Test stop_hook_active guard
    output=$(echo '{"stop_hook_active": true}' | python3 .harness/hooks/stop_completion.py 2>&1)
    if [ -z "$output" ]; then
        pass "Hook respects stop_hook_active flag"
    else
        fail "Hook should exit immediately when stop_hook_active is true"
    fi

    info "Test 3 complete"
}

# ─── Test 4: Parallel dependency grouping ───
test_parallel() {
    info "Test 4: Parallel dependency grouping"
    cd "$SCRIPT_DIR"
    python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from src.core.parallel import group_by_dependency

features = [
    {'id': '001', 'depends_on': [], 'passes': False, 'blocked': False},
    {'id': '002', 'depends_on': [], 'passes': False, 'blocked': False},
    {'id': '003', 'depends_on': ['001'], 'passes': False, 'blocked': False},
    {'id': '004', 'depends_on': ['001', '002'], 'passes': False, 'blocked': False},
    {'id': '005', 'depends_on': ['003'], 'passes': False, 'blocked': False},
]

layers = group_by_dependency(features)
assert len(layers) == 3, f'Expected 3 layers, got {len(layers)}'
assert {f['id'] for f in layers[0]} == {'001', '002'}
assert {f['id'] for f in layers[1]} == {'003', '004'}
assert {f['id'] for f in layers[2]} == {'005'}
print('Dependency grouping: 3 layers correct')
"
    pass "Parallel dependency grouping works"

    info "Test 4 complete"
}

# ─── Main ───
cd "$SCRIPT_DIR"
test_name="${1:-all}"

echo "============================================"
echo "  Long-Running Harness Integration Tests"
echo "============================================"
echo ""

case "$test_name" in
    counter)  test_counter ;;
    todo)     test_todo ;;
    hook)     test_hook ;;
    parallel) test_parallel ;;
    all)
        test_hook      # No API calls needed
        test_parallel  # No API calls needed
        info ""
        info "API-dependent tests (run in standalone terminal):"
        info "  bash test-integration.sh counter"
        info "  bash test-integration.sh todo"
        ;;
    *)
        echo "Unknown test: $test_name"
        echo "Usage: bash test-integration.sh [counter|todo|hook|parallel|all]"
        exit 1
        ;;
esac

echo ""
echo "============================================"
echo "  Integration tests complete"
echo "============================================"
