"""
Test GLM-OCR parsing via local Ollama.

Prerequisites:
    ollama pull glm-ocr:latest
    ollama serve   (if not already running)

Usage:
    uv run python ollama/test_parse.py data/raw/test_page1.pdf
    uv run python ollama/test_parse.py data/raw/figure.png
    uv run python ollama/test_parse.py data/raw/test_page1.pdf --output ./ollama/output/
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from glmocr import GlmOcr
except ImportError:
    print("ERROR: glmocr not installed. Run: uv pip install glmocr")
    sys.exit(1)

