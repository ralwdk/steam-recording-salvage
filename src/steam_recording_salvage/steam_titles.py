from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Iterable, Tuple


@dataclass(frozen=True)
class SteamTitleResult:
    appid_to_title: Dict[int, str]
    source_roots: Tuple[Path, ...]


def _get_cache_path(app_name: str = "steam_recording_salvage") -> Path:
    # I want the cache to be boring and predictable.
    # It should work on Windows + macOS without needing any extra deps.
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        # Linux or "other"
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))

    folder = base / app_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "steam_titles_cache.json"


def _load_title_cache(cache_path: Path) -> Dict[int, str]:
    # If the cache is missing or broken, we just start fresh.
    # I don't want a corrupted cache to block the app from loading sessions.
    try:
        if not cache_path.exists():
            return {}
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        out: Dict[int, str] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    appid = int(k)
                except Exception:
                    continue
                if isinstance(v, str) and v.strip():
                    out[appid] = v.strip()
        return out
    except Exception:
        return {}


def _save_title_cache(cache_path: Path, appid_to_title: Dict[int, str]) -> None:
    # Keep it human-readable so if I ever need to sanity-check it, I can.
    try:
        serializable = {str(k): v for k, v in sorted(appid_to_title.items(), key=lambda kv: kv[0])}
        cache_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # If saving fails, it's not the end of the world.
        pass


def _candidate_steam_roots() -> Iterable[Path]:
    # Steam installs differ a lot between people, so I'm intentionally trying a few "normal" spots.
    # If none exist, we just return nothing and the UI will fallback to Steam App <id>.
    home = Path.home()

    if sys.platform.startswith("win"):
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")

        yield Path(pf86) / "Steam"
        yield Path(pf) / "Steam"

        # Sometimes Steam is installed somewhere custom, but people still have this folder.
        # It's not guaranteed, but it's a free attempt.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            yield Path(local) / "Steam"
    elif sys.platform == "darwin":
        yield home / "Library" / "Application Support" / "Steam"
    else:
        # Linux common defaults
        yield home / ".steam" / "steam"
        yield home / ".local" / "share" / "Steam"


def _find_existing_roots() -> Tuple[Path, ...]:
    roots = []
    for p in _candidate_steam_roots():
        if p.exists():
            roots.append(p)
    return tuple(dict.fromkeys(roots))  # keep order, remove dupes


def _steamapps_dirs_from_root(root: Path) -> Tuple[Path, ...]:
    # This is Phase 1: I’m keeping it simple.
    # We look at the main steamapps folder and any library folders listed in libraryfolders.vdf.
    steamapps = root / "steamapps"
    dirs = []
    if steamapps.exists():
        dirs.append(steamapps)

    lib_vdf = steamapps / "libraryfolders.vdf"
    if lib_vdf.exists():
        for lib_path in _parse_libraryfolders_vdf(lib_vdf):
            sp = lib_path / "steamapps"
            if sp.exists():
                dirs.append(sp)

    return tuple(dict.fromkeys(dirs))


def _parse_libraryfolders_vdf(path: Path) -> Iterable[Path]:
    # Valve "KeyValues" format. I only care about library paths, nothing else.
    # I'm not trying to build a perfect VDF parser here, just enough to be useful offline.
    text = ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    # libraryfolders.vdf usually contains entries like:
    # "1"
    # {
    #   "path" "D:\\SteamLibrary"
    # }
    #
    # So I just scan for "path" "..."
    paths = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '"':
            key, i = _read_quoted(text, i)
            _skip_ws(text, n, i)
            if key == "path":
                # next quoted value is the path
                while i < n and text[i] != '"':
                    i += 1
                if i < n and text[i] == '"':
                    val, i = _read_quoted(text, i)
                    val = val.replace("\\\\", "\\").strip()
                    if val:
                        paths.append(Path(val))
            else:
                # skip one token-ish to keep moving
                i += 1
        else:
            i += 1
    return paths


def _read_quoted(s: str, i: int) -> Tuple[str, int]:
    # Reads a "quoted string" starting at s[i] == '"'
    i += 1
    out = []
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            # Basic escape handling, good enough for Windows paths.
            out.append(s[i + 1])
            i += 2
            continue
        if ch == '"':
            i += 1
            break
        out.append(ch)
        i += 1
    return "".join(out), i


def _skip_ws(s: str, n: int, i: int) -> int:
    while i < n and s[i].isspace():
        i += 1
    return i


def _read_appmanifest_name(appmanifest_path: Path) -> Optional[str]:
    # appmanifest_123456.acf is also KeyValues.
    # I just want the "name" field from inside it.
    try:
        text = appmanifest_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    # The manifest contains lots of keys; "name" is what we care about.
    # We'll do a lightweight quoted scan like libraryfolders.
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '"':
            key, i = _read_quoted(text, i)
            if key == "name":
                while i < n and text[i] != '"':
                    i += 1
                if i < n and text[i] == '"':
                    val, i = _read_quoted(text, i)
                    val = val.strip()
                    if val:
                        return val
        else:
            i += 1

    return None


def build_steam_appid_title_map_with_cache() -> SteamTitleResult:
    # This is the main entry point I want the GUI to call.
    #
    # What it does:
    # 1) Load cached titles (so uninstalled games still show a real name)
    # 2) Scan Steam manifests offline and update titles we can confirm
    # 3) Save merged cache back to disk
    cache_path = _get_cache_path()
    cached = _load_title_cache(cache_path)

    roots = _find_existing_roots()
    found: Dict[int, str] = {}

    for root in roots:
        for steamapps in _steamapps_dirs_from_root(root):
            for manifest in steamapps.glob("appmanifest_*.acf"):
                # appmanifest_123456.acf -> 123456
                appid = _appid_from_manifest_name(manifest.name)
                if appid is None:
                    continue

                title = _read_appmanifest_name(manifest)
                if title:
                    found[appid] = title

    # Merge: fresh found names win, cached fills the gaps.
    merged = dict(cached)
    merged.update(found)

    # Save it so even if someone uninstalls a game later, we keep the last known title.
    _save_title_cache(cache_path, merged)

    return SteamTitleResult(appid_to_title=merged, source_roots=roots)


def _appid_from_manifest_name(filename: str) -> Optional[int]:
    # appmanifest_123456.acf
    if not filename.startswith("appmanifest_") or not filename.endswith(".acf"):
        return None
    mid = filename[len("appmanifest_") : -len(".acf")]
    if not mid.isdigit():
        return None
    try:
        return int(mid)
    except Exception:
        return None
