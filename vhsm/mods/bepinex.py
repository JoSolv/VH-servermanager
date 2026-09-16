"""BepInEx / Unity Doorstop integration.

A modded Valheim server is just the stock binary launched with Doorstop
environment variables pointing at a BepInEx install. Because we pass absolute
paths, the BepInEx tree can live inside the instance directory while the game
files stay in one shared install -- the same separation r2modman draws between
a game install and a profile.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..instance import InstanceLayout

#: Thunderstore package that ships BepInEx for Valheim. Practically every mod
#: depends on it, so it is installed implicitly.
BEPINEX_PACKAGE = "denikson-BepInExPack_Valheim"
#: Folder inside that package whose *contents* are the profile root.
BEPINEX_ROOT_FOLDER = "BepInExPack_Valheim"


def is_installed(layout: InstanceLayout) -> bool:
    return (layout.bepinex / "core" / "BepInEx.Preloader.dll").is_file()


def launch_env(layout: InstanceLayout, game_dir: Path) -> dict[str, str]:
    """Environment variables that make the stock binary load BepInEx.

    Doorstop 3 and Doorstop 4 read different variable names and BepInEx packs
    ship either one, so we set both. The unused set is simply ignored.
    """
    preloader = layout.bepinex / "core" / "BepInEx.Preloader.dll"
    env: dict[str, str] = {
        # Doorstop 3.x (what the current Valheim BepInEx pack ships).
        "DOORSTOP_ENABLE": "TRUE",
        "DOORSTOP_INVOKE_DLL_PATH": str(preloader),
        # Doorstop 4.x.
        "DOORSTOP_ENABLED": "1",
        "DOORSTOP_TARGET_ASSEMBLY": str(preloader),
    }

    corlib = layout.root / "unstripped_corlib"
    if corlib.is_dir():
        env["DOORSTOP_CORLIB_OVERRIDE_PATH"] = str(corlib)
        env["DOORSTOP_MONO_LIB_DIR"] = str(corlib)

    lib_paths = [str(layout.doorstop_libs), str(game_dir / "linux64"), str(game_dir)]
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    if existing:
        lib_paths.append(existing)
    env["LD_LIBRARY_PATH"] = ":".join(lib_paths)

    doorstop_so = layout.doorstop_libs / "libdoorstop_x64.so"
    if doorstop_so.is_file():
        preload = [str(doorstop_so)]
        if os.environ.get("LD_PRELOAD"):
            preload.append(os.environ["LD_PRELOAD"])
        env["LD_PRELOAD"] = ":".join(preload)
    return env
