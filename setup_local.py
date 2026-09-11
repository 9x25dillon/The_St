#!/usr/bin/env python3
"""Explicit optional installation; never runs as part of an import."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description='Install optional semantic analysis locally')
parser.add_argument('--download-model', action='store_true', help='download the embedding model for subsequent offline use')
args = parser.parse_args()
python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
try:
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(root / '.venv')
    if args.download_model:
        subprocess.run([str(python), '-c', "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"], check=True)
    else:
        if sys.platform.startswith('linux') or sys.platform == 'win32':
            subprocess.run([str(python), '-m', 'pip', 'install', 'torch',
                            '--index-url', 'https://download.pytorch.org/whl/cpu'], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(root / 'requirements.txt')], check=True)
    print('Ready. Start the app with: python run.py')
except (OSError, subprocess.CalledProcessError) as exc:
    parser.exit(1, f'Setup failed: {exc}\nCheck your connection and retry. The basic app still runs with python app.py.\n')
