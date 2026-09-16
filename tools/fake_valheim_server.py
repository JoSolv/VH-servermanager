#!/usr/bin/env python3
"""A stand-in for valheim_server.x86_64, used for development.

It speaks just enough of the real server's behaviour to exercise the manager
end to end without a 2 GB Steam download: Valheim-shaped console output, a
working Steam A2S query socket, simulated players joining and leaving, and a
graceful SIGINT shutdown. Enable with VHSM_FAKE_SERVER=1.
"""

from __future__ import annotations

import argparse
import random
import signal
import socket
import struct
import sys
import threading
import time

NAMES = ["Bjorn", "Astrid", "Leif", "Sigrun", "Hrafn", "Yrsa", "Ulf", "Thora"]
running = True
players: dict[str, str] = {}          # steam id -> character name
lock = threading.Lock()


def log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------- #
# Steam A2S responder
# --------------------------------------------------------------------------- #
def _cstr(value: str) -> bytes:
    return value.encode("utf-8", errors="replace") + b"\x00"


def a2s_server(port: int, name: str, world: str, public: bool) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        log(f"Failed to bind query port {port}: {exc}")
        return
    sock.settimeout(0.5)
    log(f"Query socket listening on {port}")

    challenge = struct.pack("<i", random.randint(1, 2**31 - 1))
    while running:
        try:
            data, addr = sock.recvfrom(1400)
        except socket.timeout:
            continue
        except OSError:
            break
        if not data.startswith(b"\xff\xff\xff\xff") or len(data) < 5:
            continue
        kind = data[4:5]

        with lock:
            snapshot = dict(players)

        if kind == b"\x54":  # A2S_INFO
            body = b"\xff\xff\xff\xffI" + bytes([17])
            body += _cstr(name) + _cstr(world) + _cstr("valheim") + _cstr("Valheim")
            body += struct.pack("<H", 892970 & 0xFFFF)  # A2S ID is 16-bit
            body += bytes([len(snapshot), 64, 0])
            body += b"d" + b"l" + bytes([0 if public else 1, 0])
            body += _cstr("0.217.46") + bytes([0])
            sock.sendto(body, addr)
        elif kind == b"\x55":  # A2S_PLAYERS
            if data[5:9] != challenge:
                sock.sendto(b"\xff\xff\xff\xffA" + challenge, addr)
                continue
            body = b"\xff\xff\xff\xffD" + bytes([len(snapshot)])
            for index, player in enumerate(snapshot.values()):
                body += bytes([index]) + _cstr(player)
                body += struct.pack("<i", 0) + struct.pack("<f", random.uniform(60, 3600))
            sock.sendto(body, addr)
    sock.close()


# --------------------------------------------------------------------------- #
# Simulated player churn
# --------------------------------------------------------------------------- #
def player_churn() -> None:
    while running:
        time.sleep(random.uniform(8, 20))
        if not running:
            return
        with lock:
            joining = len(players) < 4 and (not players or random.random() < 0.6)
            if joining:
                steam_id = str(random.randint(76561197960265728, 76561199999999999))
                name = random.choice([n for n in NAMES if n not in players.values()] or NAMES)
                players[steam_id] = name
            else:
                steam_id, name = random.choice(list(players.items()))
                del players[steam_id]
            count = len(players)
        if joining:
            log(f"Got connection SteamID {steam_id}")
            log(f"Got handshake from client {steam_id}")
            log(f"Got character ZDOID from {name} : {random.randint(1, 10**9)}:1")
        else:
            log(f"Closing socket {steam_id}")
        log(f"Connections {count} ZDOS:{count * 137} sent:0 recv:0")


def main() -> int:
    global running

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-name", default="Fake Server")
    parser.add_argument("-world", default="Dedicated")
    parser.add_argument("-port", type=int, default=2456)
    parser.add_argument("-public", default="0")
    parser.add_argument("-savedir", default=".")
    known, _ = parser.parse_known_args()

    def handle_stop(signum, _frame):
        global running
        log(f"Received signal {signum}, shutting down")
        running = False

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    log("Valheim version: l-0.217.46 (fake)")
    log(f"Starting to load scene:main using world {known.world!r}")
    threading.Thread(
        target=a2s_server,
        args=(known.port + 1, known.name, known.world, known.public == "1"),
        daemon=True,
    ).start()
    threading.Thread(target=player_churn, daemon=True).start()
    time.sleep(1.5)
    log("Game server connected")
    log(f"DungeonDB Start {random.randint(1000, 9999)}")
    log("Session \"%s\" registered with Steam" % known.name)

    ballast = bytearray(48 * 1024 * 1024)  # resemble a real server's RSS
    while running:
        # A little steady CPU so the metrics graphs are not flat lines.
        sum(i * i for i in range(20000))
        ballast[random.randrange(len(ballast))] = random.randrange(256)
        time.sleep(0.25)

    log("World saved ( 42.13ms )")
    log("Shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
