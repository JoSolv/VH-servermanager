#!/usr/bin/env python3
"""Build a Valheim world on disk for testing, in either save format.

    python tools/make_world.py <worlds_local dir> <Name> [--legacy] [--generations N]

A 1.0 world is a folder of ``_main.<N>.db2`` / ``.fwl2`` / ``.chunks`` / ``.ok``
plus terrain ``.chunk`` files; the legacy format is a ``.db`` + ``.fwl`` pair.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path


def make_folder_world(parent: Path, name: str, generations: int = 2, chunks: int = 12,
                      incomplete: bool = False) -> Path:
    world = parent / name
    world.mkdir(parents=True, exist_ok=True)
    for n in range(generations):
        (world / f"_main.{n}.db2").write_bytes(b"DB2" + bytes(random.getrandbits(8) for _ in range(64)))
        (world / f"_main.{n}.fwl2").write_bytes(b"FWL2seed" + name.encode())
        (world / f"_main.{n}.chunks").write_bytes(b"INDEX")
        # The .ok marker is written last; omit it to mimic an interrupted save.
        if not (incomplete and n == generations - 1):
            (world / f"_main.{n}.ok").write_bytes(b"")
    for i in range(chunks):
        (world / f"{i:04x}{random.getrandbits(16):04x}.chunk").write_bytes(
            bytes(random.getrandbits(8) for _ in range(128))
        )
    return world


def make_pair_world(parent: Path, name: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    db = parent / f"{name}.db"
    db.write_bytes(b"LEGACY-DB-" + name.encode())
    (parent / f"{name}.fwl").write_bytes(b"LEGACY-FWL")
    return db


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("name")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--generations", type=int, default=2)
    parser.add_argument("--incomplete", action="store_true")
    args = parser.parse_args()

    parent = Path(args.directory)
    if args.legacy:
        print(make_pair_world(parent, args.name))
    else:
        print(make_folder_world(parent, args.name, args.generations, incomplete=args.incomplete))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
