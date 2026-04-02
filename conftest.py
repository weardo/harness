import sys
from pathlib import Path

# Add src to path so tests can import from src.core.*
sys.path.insert(0, str(Path(__file__).parent))
