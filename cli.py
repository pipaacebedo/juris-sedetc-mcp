#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cli.py: entry point del script tc-juris-mcp (equivale a python server.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import main

if __name__ == "__main__":
    main()
