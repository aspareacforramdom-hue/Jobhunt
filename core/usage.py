"""Local token accounting. Stores counts only, never prompts or credentials."""
import sqlite3
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'data/shivani/llm-usage.db'


def record(model, usage):
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(PATH, timeout=5) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS calls (created TEXT DEFAULT CURRENT_TIMESTAMP, model TEXT, input INTEGER, output INTEGER, cached INTEGER, cache_write INTEGER)')
            conn.execute('INSERT INTO calls (model,input,output,cached,cache_write) VALUES (?,?,?,?,?)',
                         (model, usage.get('input_tokens', 0), usage.get('output_tokens', 0),
                          usage.get('cache_read_input_tokens', 0), usage.get('cache_creation_input_tokens', 0)))
    except (sqlite3.Error, OSError):
        print('  [usage] Token counter unavailable; scoring result retained')
