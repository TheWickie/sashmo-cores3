"""Absolute-path launcher used by the optional Windows Run entry."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deck.app import main

main()
