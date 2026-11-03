"""Election results layer for the narrative tracker.

Polls the Texas Secretary of State's election-night returns (the Civix IVIS backend behind
results.texas-election.com), stores every change as a timestamped snapshot, and exports JSON the
dashboard reads. See election/README.md for the layout and the full design.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "election"
OUT_DIR = ROOT / "out" / "election"
REGISTRY_PATH = Path(__file__).resolve().parent / "registry.yaml"
