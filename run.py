#!/usr/bin/env python3
"""Prefer the optional project environment; otherwise use standard-library mode."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
if python.exists() and Path(sys.prefix).resolve() != (root / '.venv').resolve():
    os.execv(str(python), [str(python), str(root / 'app.py'), *sys.argv[1:]])
else:
    from app import main
    main()
