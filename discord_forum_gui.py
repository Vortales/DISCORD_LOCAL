#!/usr/bin/env python3
"""Compatibility launcher for the latest versioned GUI."""

from pathlib import Path
import runpy


LATEST_GUI = Path(__file__).resolve().parent / "v4" / "discord_forum_gui.py"

if __name__ == "__main__":
    runpy.run_path(str(LATEST_GUI), run_name="__main__")
