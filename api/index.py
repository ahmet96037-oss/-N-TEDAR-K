"""Vercel Serverless Function — FastAPI wrapper."""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.api import app

# Export for Vercel
handler = app
