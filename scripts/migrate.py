#!/usr/bin/env python3
"""Compatibility entry point for the packaged migration command."""
import sys
from plumbline.cli import app

if __name__ == "__main__":
    app(args=["db-status"] if "--status" in sys.argv[1:] else ["migrate", *sys.argv[1:]])
