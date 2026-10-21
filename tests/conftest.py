import os
from pathlib import Path

# Tests always use the committed example roster, never a developer's local one.
os.environ["ROSTER_PATH"] = str(Path(__file__).resolve().parents[1] / "config" / "roster.example.json")
