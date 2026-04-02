#!/usr/bin/env python3
"""
Stop Hook — Mode B Completion Check
=====================================

Claude Code Stop hook that prevents exit until:
- All features in feature_list.json pass (or are blocked)
- Test suite passes
- Circuit breaker hasn't tripped (stagnation detection)

Install in .claude/settings.json:
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python .harness/hooks/stop_completion.py"
      }]
    }]
  }
}
"""

import json
import sys
from pathlib import Path

# Add parent dirs to path
hook_dir = Path(__file__).parent
harness_dir = hook_dir.parent
sys.path.insert(0, str(harness_dir))

from core.completion import check_completion
from core.circuit_breaker import CircuitBreaker


def main():
    # Read hook input from stdin
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)  # Can't parse input, allow exit

    # Prevent infinite loop — if we're already in forced continuation, allow exit
    if input_data.get("stop_hook_active", False):
        sys.exit(0)

    # Find harness state directory
    state_dir = harness_dir / "state"
    if not state_dir.exists():
        sys.exit(0)  # No harness state, allow exit

    # Check circuit breaker — if OPEN, don't force continuation (stagnation)
    cb = CircuitBreaker(state_dir)
    if cb.state == "OPEN":
        sys.exit(0)  # Stagnation detected, let Claude stop

    # Check completion
    result = check_completion(harness_dir)

    if result["complete"]:
        sys.exit(0)  # All done, allow exit

    # Not complete — block exit
    cb.record_iteration(progress=result.get("progress", False))

    # If circuit breaker just tripped, allow exit
    if cb.state == "OPEN":
        sys.exit(0)

    # Block exit with reason
    output = {
        "decision": "block",
        "reason": result["reason"],
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
