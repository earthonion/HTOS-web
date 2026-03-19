#!/usr/bin/env python3
"""Extract exported functions from PS5 SPRX libraries using NID lookup.
Uses aerolib.csv for NID->symbol resolution and pyelftools for ELF parsing."""

import os
import sqlite3
import sys

DB_PATH = os.getenv("FUNCS_DB_PATH", "functions.db")


def load_nid_map(csv_path):
    """Load NID -> symbol name mapping from aerolib.csv."""
    nid_map = {}
    with open(csv_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(' ', 1)
            if len(parts) == 2:
                nid_map[parts[0]] = parts[1]
    return nid_map


def extract_exports(filepath, nid_map):
    """Extract exported function and object names from a SPRX ELF using NID table."""
    from elftools.elf.elffile import ELFFile

    funcs = set()
    try:
        with open(filepath, 'rb') as f:
            elf = ELFFile(f)
            for segment in elf.iter_segments():
                if segment.header.p_type != 'PT_DYNAMIC':
                    continue
                for sym in segment.iter_symbols():
                    if sym.entry['st_shndx'] == 'SHN_UNDEF':
                        continue
                    if not sym.name:
                        continue
                    sym_type = sym.entry['st_info']['type']
                    if sym_type not in ('STT_FUNC', 'STT_OBJECT'):
                        continue
                    # NID format: nid#lid#mid
                    parts = sym.name.split('#')
                    if len(parts) >= 1:
                        nid = parts[0]
                        if nid in nid_map:
                            funcs.add((nid_map[nid], sym_type))
    except Exception as e:
        print(f"  Warning: {filepath}: {e}")
    return funcs


def build():
    libs_dir = sys.argv[1] if len(sys.argv) > 1 else None
    aerolib_path = sys.argv[2] if len(sys.argv) > 2 else None

    if not libs_dir or not aerolib_path:
        print("Usage: build_funcs_db.py <libs_directory> <aerolib.csv>")
        sys.exit(1)

    print(f"  Loading NID map from {aerolib_path}...")
    nid_map = load_nid_map(aerolib_path)
    print(f"  {len(nid_map)} NIDs loaded")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS functions")
    conn.execute("""
        CREATE TABLE functions (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            library TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'function'
        )
    """)
    conn.execute("CREATE INDEX idx_func_name ON functions(name)")
    conn.execute("CREATE INDEX idx_func_library ON functions(library)")

    total = 0
    files = sorted(f for f in os.listdir(libs_dir) if f.endswith(".dec") or f.endswith(".sprx"))
    for fname in files:
        filepath = os.path.join(libs_dir, fname)
        lib_name = fname.replace(".dec", "")

        exports = extract_exports(filepath, nid_map)
        if exports:
            print(f"  {lib_name}: {len(exports)} exports")
            for name, sym_type in exports:
                ftype = "function" if sym_type == "STT_FUNC" else "object"
                conn.execute(
                    "INSERT INTO functions (name, library, type) VALUES (?, ?, ?)",
                    (name, lib_name, ftype)
                )
            total += len(exports)

    conn.commit()
    conn.execute("PRAGMA optimize")
    conn.close()
    print(f"  Done. {total} exports from {len(files)} libraries in {DB_PATH}")


if __name__ == "__main__":
    build()
