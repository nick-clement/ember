#!/bin/bash
# Quick test without building the app
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -q pyhive-integration rumps keyring pillow
python ember.py
