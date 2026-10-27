#!/usr/bin/env python3
"""Capture design-review screenshots and the axe contrast report on disposable storage.

Usage: scripts/design_screenshots.py [LABEL] [--dark] [--strict]
Writes PNGs and contrast.json to docs/design/screenshots/LABEL (default "after").
Build the frontend first (cd frontend && npm run build).
"""
import os
import sys

from test_ui import run_file


def main():
    args = sys.argv[1:]
    os.environ['SHOT_LABEL'] = next((arg for arg in args if not arg.startswith('--')), 'after')
    os.environ['SHOT_DARK'] = '1' if '--dark' in args else '0'
    os.environ['CONTRAST_STRICT'] = '1' if '--strict' in args else '0'
    return run_file('visual', ['npx', 'playwright', 'test', '--config', 'visual/playwright.config.js'])


if __name__ == '__main__':
    sys.exit(main())
