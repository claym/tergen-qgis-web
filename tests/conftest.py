import sys
from pathlib import Path

# Make chart/files importable as if it were a package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chart" / "files"))
