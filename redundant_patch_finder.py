#!/usr/bin/env python3
"""
Redundant Patch Finder for Skyrim SE / AE  (Mod Organizer 2)

Reads the header of every ACTIVE plugin in your MO2 profile and flags:
  1. Patches that require exactly the same set of mods (likely duplicates),
     e.g. "Lux - Obscure College.esp" and "Obscure College - Lux.esp".
  2. Plugins whose master (required plugin) is missing or disabled.

It only READS files. It never changes your load order or any plugin.
No third-party packages needed (Python 3.8+).
"""
import argparse
import json
import mmap
import zlib
import os
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

VERSION = "0.3.0"
MAX_PATCH_RECORDS = 4000   # plugins bigger than this are mods, not patches; skipped in record scan
HUB_LIMIT = 150            # a record edited by more plugins than this is ignored (noise)
PLUGIN_EXTS = {".esp", ".esm", ".esl"}
OFFICIAL = {"skyrim.esm", "update.esm", "dawnguard.esm", "hearthfires.esm", "dragonborn.esm",
            "_resourcepack.esl"}  # _ResourcePack.esl ships with Anniversary Edition
# Creation Club files look like ccBGSSSE001-Fish.esm, ccqdrsse001-survivalmode.esl, ...
CC_RE = re.compile(r"^cc[a-z]+sse\d+[-_]", re.IGNORECASE)
EXTRA_CC = set()  # filled from Skyrim.ccc when the game folder is known
# Real names for some Creation Club files (partial list; unknown ones just say "Creation Club").
CC_NAMES = {
    "cckrtsse001_altar.esl": "Bittercup",
    "ccbgssse064-ba_elven.esl": "Alternative Armors - Elven Hunter",
    "ccbgssse001-fish.esm": "Fishing",
    "ccbgssse025-advdsgs.esm": "Saints and Seducers",
    "ccbgssse067-daedinv.esm": "The Cause",
    "cceejsse001-hstead.esm": "Tundra Homestead",
    "cceejsse005-cave.esm": "Bloodchill Manor",
    "ccbgssse031-advcyrus.esm": "Dead Man's Dread",
    "ccbgssse016-umbra.esm": "Umbra",
    "ccasvsse001-almsivi.esm": "Ghosts of the Tribunal",
    "cctwbsse001-puzzledungeon.esm": "Forgotten Seasons",
    "ccafdsse001-dwesanctuary.esm": "Nchuanthumz Dwarven Home",
}
STOP_WORDS = {"patch", "patches", "compatibility", "compat", "and", "for", "the", "plus",
              "esp", "esm", "esl", "se", "ae", "sse", "fix", "fixes"}


# --------------------------------------------------------------------------- helpers
def read_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def is_official(name):
    return name.lower() in OFFICIAL


def is_cc(name):
    n = name.lower()
    return n in EXTRA_CC or bool(CC_RE.match(n))


def is_vanilla(name):
    """Base game, DLC, AE resource pack or any Creation Club file."""
    return is_official(name) or is_cc(name)


def load_ccc(data_dir):
    """Skyrim.ccc lists every Creation Club plugin the game loads automatically."""
    try:
        f = Path(data_dir) / "Skyrim.ccc"
        if f.is_file():
            EXTRA_CC.update(l.strip().lower() for l in read_text(f).splitlines() if l.strip())
    except OSError:
        pass


def name_tokens(plugin_name):
    base = re.sub(r"['\u2019]s\b", "", os.path.splitext(plugin_name)[0].lower())  # JK's -> jk
    words = re.split(r"[^a-z0-9]+", base)
    return frozenset(w for w in words if w and w not in STOP_WORDS)


# --------------------------------------------------------------------------- plugin header
def read_header(path):
    """Return dict with masters, flags and record count from a plugin's TES4 header."""
    with open(path, "rb") as f:
        head = f.read(24)
        if len(head) < 24 or head[:4] != b"TES4":
            raise ValueError("not a Skyrim plugin (no TES4 header)")
        size, flags = struct.unpack("<II", head[4:12])
        data = f.read(size)
    masters, num_records, pos, author, desc = [], None, 0, "", ""
    while pos + 6 <= len(data):
        sub = data[pos:pos + 4]
        sub_size = struct.unpack("<H", data[pos + 4:pos + 6])[0]
        body = data[pos + 6:pos + 6 + sub_size]
        pos += 6 + sub_size
        if sub == b"MAST":
            masters.append(body.split(b"\x00")[0].decode("cp1252", "replace"))
        elif sub == b"CNAM":
            author = body.split(b"\x00")[0].decode("cp1252", "replace")
        elif sub == b"SNAM":
            desc = body.split(b"\x00")[0].decode("cp1252", "replace")
        elif sub == b"HEDR" and len(body) >= 12:
            num_records = struct.unpack("<I", body[4:8])[0]
    ext = os.path.splitext(str(path))[1].lower()
    return {
        "masters": masters,
        "is_master_flag": bool(flags & 0x1),
        "is_light": bool(flags & 0x200) or ext == ".esl",
        "num_records": num_records,
        "author": author,
        "description": desc,
    }


def scan_overrides(path, masters):
    """Which existing records does this plugin EDIT? Returns {(master_lower, formid): 'TYPE'}.
    Walks only the 24-byte record headers, so it is fast and never reads record contents."""
    data = Path(path).read_bytes()
    if data[:4] != b"TES4":
        raise ValueError("not a Skyrim plugin")
    pos = 24 + struct.unpack("<I", data[4:8])[0]
    n_masters, out, end = len(masters), {}, len(data)
    low = [m.lower() for m in masters]
    while pos + 24 <= end:
        rtype = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if rtype == b"GRUP":
            pos += 24                       # step INTO the group; its records follow directly
            continue
        flags, fid = struct.unpack("<II", data[pos + 8:pos + 16])
        idx = fid >> 24
        if idx < n_masters and not (flags & 0x20):      # 0x20 = deleted marker, ignore
            out[(low[idx], fid & 0xFFFFFF)] = (rtype.decode("ascii", "replace"), pos)
        pos += 24 + size
    return out


# --------------------------------------------------------------------------- MO2 discovery
def parse_mo_ini(ini_path):
    vals = {}
    for line in read_text(ini_path).splitlines():
        s = line.strip()
        if not s or s.startswith((";", "[")) or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        m = re.match(r"@ByteArray\((.*)\)$", v)
        if m:
            v = m.group(1)
        vals[k.strip()] = v.replace("\\\\", "\\")
    return vals


def find_instances():
    found, seen = [], set()
    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents)[:2]:
        if (p / "ModOrganizer.ini").is_file() and p not in seen:
            seen.add(p); found.append(p)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for ini in sorted(Path(local, "ModOrganizer").glob("*/ModOrganizer.ini")):
            if ini.parent not in seen:
                seen.add(ini.parent); found.append(ini.parent)
    return found


def locate_dirs(instance):
    ini = parse_mo_ini(instance / "ModOrganizer.ini")
    base = ini.get("base_directory", "").replace("%BASE_DIR%", str(instance)) or str(instance)
    base = Path(base)
    if not (base / "mods").is_dir():
        base = instance
    game_path = ini.get("gamePath", "")
    game_data = Path(game_path) / "Data" if game_path else None
    return base, ini.get("selected_profile", "Default"), game_data


def parse_plugins_txt(pfile):
    """Returns (active_names_in_order, inactive_names). '*' prefix = active."""
    active, inactive = [], []
    for line in read_text(pfile).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("*"):
            active.append(s[1:])
        else:
            inactive.append(s)
    return active, inactive


def load_active_plugins(profile_dir):
    pfile = profile_dir / "plugins.txt"
    if not pfile.is_file():
        raise SystemExit("Could not find %s" % pfile)
    return parse_plugins_txt(pfile)


# --------------------------------------------------------------------------- non-MO2 setups
def plugins_txt_candidates():
    """plugins.txt written by the game/Vortex/Wrye Bash (Windows AppData, or Steam Deck/Linux Proton)."""
    found = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found += sorted(Path(local).glob("Skyrim Special Edition*/plugins.txt"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    found.append(Path.home() / ".local/share/Steam/steamapps/compatdata/489830/pfx/drive_c/users/"
                 "steamuser/AppData/Local/Skyrim Special Edition/plugins.txt")
    return [p for p in found if p.is_file()]


def game_data_candidates():
    """Likely Skyrim Data folders (Steam / GOG / registry / Linux)."""
    found = []
    try:
        import winreg  # Windows only
        for key in (r"SOFTWARE\WOW6432Node\Bethesda Softworks\Skyrim Special Edition",
                    r"SOFTWARE\Bethesda Softworks\Skyrim Special Edition"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    found.append(Path(winreg.QueryValueEx(k, "installed path")[0]) / "Data")
            except OSError:
                pass
    except ImportError:
        pass
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(var)
        if base:
            found += [Path(base) / "Steam/steamapps/common/Skyrim Special Edition/Data",
                      Path(base) / "GOG Galaxy/Games/Skyrim Anniversary Edition/Data",
                      Path(base) / "GOG Galaxy/Games/Skyrim Special Edition/Data"]
    found.append(Path.home() / ".local/share/Steam/steamapps/common/Skyrim Special Edition/Data")
    unique, seen = [], set()
    for p in found:
        if p.is_dir() and p not in seen:
            seen.add(p); unique.append(p)
    return unique


def build_flat_index(data_dir):
    """Plugins sitting directly in a Data folder (Vortex, Wrye Bash, manual installs)."""
    index = {}
    for f in data_dir.iterdir():
        name = f.name[:-6] if f.name.lower().endswith(".ghost") else f.name  # Wrye Bash ghosts
        if f.is_file() and os.path.splitext(name)[1].lower() in PLUGIN_EXTS:
            index[name.lower()] = f
    return index


def build_file_index(base, profile_dir, game_data):
    """Map lowercase plugin filename -> real path, honouring MO2 mod priority."""
    index = {}

    def add_dir(d):
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in PLUGIN_EXTS:
                    index.setdefault(f.name.lower(), f)

    add_dir(base / "overwrite")  # highest priority
    mods_dir = base / "mods"
    ordered = []
    modlist = profile_dir / "modlist.txt"
    if modlist.is_file():
        # MO2 writes modlist.txt with the highest-priority mod FIRST.
        for line in read_text(modlist).splitlines():
            if line.startswith("+"):
                ordered.append(line[1:].strip())
    else:
        ordered = sorted(p.name for p in mods_dir.iterdir() if p.is_dir())
    for name in ordered:
        add_dir(mods_dir / name)
    if game_data:
        add_dir(game_data)
    return index


# --------------------------------------------------------------------------- analysis
def analyse(plugins, active_names, inactive_names, min_masters, available=frozenset(),
            cc_check=False):
    """plugins: {name: header dict}. Returns a list of findings (dicts)."""
    findings = []
    active_lower = {n.lower() for n in active_names}
    inactive_lower = {n.lower() for n in inactive_names}
    pretty = {}
    for name, h in plugins.items():
        for m in h["masters"]:
            pretty.setdefault(m.lower(), m)

    def patched_mods(h):
        return frozenset(m.lower() for m in h["masters"] if not is_vanilla(m))

    # 1. same set of patched mods + matching names  -> HIGH (or MEDIUM if names differ)
    groups = defaultdict(list)
    for name, h in plugins.items():
        pm = patched_mods(h)
        if len(pm) >= min_masters:
            groups[pm].append(name)
    in_high = set()
    for masters, members in groups.items():
        if len(members) < 2:
            continue
        by_tokens = defaultdict(list)
        for m in members:
            by_tokens[name_tokens(m)].append(m)
        high = [v for v in by_tokens.values() if len(v) >= 2]
        shown = [{"confidence": "HIGH", "plugins": sorted(v)} for v in high]
        for v in high:
            in_high.update(v)
        if not high:
            shown = [{"confidence": "MEDIUM", "plugins": sorted(members)}]
        for sh in shown:
            sh.update(kind="duplicate_patch", requires=sorted(pretty[m] for m in masters))
            findings.append(sh)

    # 2. same name (words swapped) but they require DIFFERENT mods -> NAME
    by_name = defaultdict(list)
    for name, h in plugins.items():
        tok = name_tokens(name)
        if len(tok) >= 2 and patched_mods(h):
            by_name[tok].append(name)
    for tok, members in by_name.items():
        members = [m for m in members if m not in in_high]
        if len(members) >= 2:
            sets = {patched_mods(plugins[m]) for m in members}
            if len(sets) > 1:
                findings.append({"kind": "duplicate_patch", "confidence": "NAME",
                                 "plugins": sorted(members),
                                 "requires": sorted({pretty[x] for st in sets for x in st})})

    # 3. missing masters
    for name, h in plugins.items():
        for m in h["masters"]:
            ml = m.lower()
            if is_official(m) or ml in active_lower:
                continue
            if is_cc(m):
                if ml in available or not cc_check:
                    continue  # Creation Club files load automatically; assume present
                label = CC_NAMES.get(ml)
                reason = "Creation Club file not installed" + (": " + label if label else "")
            elif ml in inactive_lower or ml in available:
                reason = "installed but NOT enabled"
            else:
                reason = "NOT installed"
            findings.append({"kind": "missing_master", "confidence": "HIGH",
                             "plugins": [name], "master": m, "reason": reason})
    return findings


FIELD_NAMES = {"VMAD": "attached scripts", "EDID": "editor ID", "FULL": "display name", "DATA": "main data",
               "XCLL": "lighting", "XCLC": "cell grid", "XCWT": "water", "XEZN": "encounter zone",
               "XLCN": "location", "XOWN": "owner", "LTMP": "lighting template", "NAME": "base object",
               "XESP": "enable-parent", "XLOC": "lock", "XSCL": "scale", "XMRK": "map marker",
               "OBND": "object bounds", "MODL": "model", "DESC": "description", "KWDA": "keywords",
               "CNTO": "container items", "XPRM": "primitive", "XRNK": "faction rank", "XLRT": "location ref type",
               "XLRL": "location ref", "XAPD": "activate parents", "XTEL": "teleport door", "XOCP": "occlusion plane"}


def parse_record(buf, off):
    """Return {subrecord type: [bytes, ...]} for the record starting at buf[off]."""
    size, flags = struct.unpack_from("<II", buf, off + 4)
    data = bytes(buf[off + 24: off + 24 + size])
    if flags & 0x00040000:                       # compressed record
        data = zlib.decompress(data[4:])
    out, pos, big = defaultdict(list), 0, None
    while pos + 6 <= len(data):
        t = data[pos:pos + 4]
        sz = struct.unpack_from("<H", data, pos + 4)[0]
        if t == b"XXXX":                         # oversized-subrecord marker
            big = struct.unpack_from("<I", data, pos + 6)[0]
            pos += 6 + sz
            continue
        if big is not None:
            sz, big = big, None
        out[t.decode("ascii", "replace")].append(data[pos + 6: pos + 6 + sz])
        pos += 6 + sz
    return out


def same_value(a, b, ma, na, mb, nb):
    """Equal ignoring the master numbering. FormIDs inside records are numbered per plugin, so the
    same reference can differ in one byte between two plugins. We accept a difference only when the
    two numbers point at the SAME master file."""
    if a == b:
        return True
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        nx = ma[x] if x < len(ma) else (na if x == len(ma) else None)
        ny = mb[y] if y < len(mb) else (nb if y == len(mb) else None)
        if nx is None or nx != ny:
            return False
    return True


def lists_same(la, lb, ma, na, mb, nb):
    return len(la) == len(lb) and all(same_value(x, y, ma, na, mb, nb) for x, y in zip(la, lb))


def judge_pair(a, b, keys, ctx):
    """Does the LATER patch b throw away a change the EARLIER patch a made?"""
    conflicts, harmless, unverified = [], defaultdict(int), 0
    ma, mb = ctx["masters"][a], ctx["masters"][b]
    for key in keys:
        ra = parse_record(ctx["buf"](a), ctx["edits"][a][key][1])
        rb = parse_record(ctx["buf"](b), ctx["edits"][b][key][1])
        rtype = ctx["edits"][a][key][0]
        types = set(ra) | set(rb)
        if all(lists_same(ra.get(t, []), rb.get(t, []), ma, a.lower(), mb, b.lower()) for t in types):
            harmless["both patches contain identical data"] += 1
            continue
        base = ctx["base"](key)
        if base is None:
            unverified += 1
            continue
        rbase, mbase, nbase = base
        lost = []
        for t in types:
            changed_by_a = not lists_same(ra.get(t, []), rbase.get(t, []), ma, a.lower(), mbase, nbase)
            changed_by_b = not lists_same(rb.get(t, []), rbase.get(t, []), mb, b.lower(), mbase, nbase)
            if not (changed_by_a and changed_by_b):
                continue          # only one of them touches this field: no clash
            if not lists_same(ra.get(t, []), rb.get(t, []), ma, a.lower(), mb, b.lower()):
                lost.append(t)    # both changed it, to different values: only one can win
        if lost:
            conflicts.append({"record": rtype, "formid": "%06X" % key[1], "master": key[0],
                              "lost": sorted(lost)})
        elif all(lists_same(ra.get(t, []), rbase.get(t, []), ma, a.lower(), mbase, nbase) for t in types):
            harmless["the first patch changes nothing compared with the original"] += 1
        else:
            harmless["they change different things, or the later one carries the changes forward"] += 1
    return conflicts, dict(harmless), unverified


class BaseStore:
    """Looks up the ORIGINAL version of a record in the plugin that created it."""
    def __init__(self, index, wanted):
        self.index, self.found, self.maps, self.meta = index, {}, {}, {}
        by_master = defaultdict(set)
        for master, lid in wanted:
            by_master[master].add(lid)
        for master, lids in by_master.items():
            path = index.get(master)
            if path is None:
                continue
            try:
                masters = read_header(path)["masters"]
                f = open(path, "rb")
                buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                pos, end, n, left = 24 + struct.unpack_from("<I", buf, 4)[0], len(buf), len(masters), set(lids)
                while pos + 24 <= end and left:
                    if buf[pos:pos + 4] == b"GRUP":
                        pos += 24
                        continue
                    size, _fl, fid = struct.unpack_from("<III", buf, pos + 4)
                    if (fid >> 24) == n and (fid & 0xFFFFFF) in left:
                        left.discard(fid & 0xFFFFFF)
                        self.found[(master, fid & 0xFFFFFF)] = pos
                    pos += 24 + size
                self.maps[master] = buf
                self.meta[master] = ([m.lower() for m in masters], master)
            except Exception:  # noqa: BLE001 - an unreadable master just means "unverified"
                continue

    def __call__(self, key):
        pos = self.found.get(key)
        if pos is None:
            return None
        try:
            rec = parse_record(self.maps[key[0]], pos)
        except Exception:  # noqa: BLE001
            return None
        masters, name = self.meta[key[0]]
        return rec, masters, name


def analyse_overlaps(plugins, active, index, min_masters, max_records=MAX_PATCH_RECORDS, progress=None,
                     mod_of=None, loose=False):
    """Find patches that edit the same records, then check whether the later one really discards
    something the earlier one changed. Returns (overlaps, scanned_count, hub_records, skipped_big)."""
    order = {n: i for i, n in enumerate(active)}
    cands = [n for n, h in plugins.items()
             if len([m for m in h["masters"] if not is_vanilla(m)]) >= min_masters]
    edits, skipped_big = {}, 0
    for k, n in enumerate(cands):
        h = plugins[n]
        if h.get("num_records") and h["num_records"] > max_records:
            skipped_big += 1
            continue
        try:
            e = scan_overrides(index[n.lower()], h["masters"])
        except Exception:  # noqa: BLE001
            continue
        if e:
            edits[n] = e
        if progress and k % 25 == 0:
            progress("Reading patch contents... %d/%d" % (k, len(cands)))
    holders = defaultdict(list)
    for n, e in edits.items():
        for key in e:
            holders[key].append(n)
    targets = {n: frozenset(m.lower() for m in plugins[n]["masters"] if not is_vanilla(m)) for n in edits}
    reasons = defaultdict(int)

    def competing(a, b):
        """Two patches COMPETE when they do the same job for the same mods and neither is built on the other."""
        if a.lower() in targets[b] or b.lower() in targets[a]:
            reasons["one patch is built on top of the other"] += 1
            return False
        if mod_of and mod_of(a) and mod_of(a) == mod_of(b):
            reasons["both come from the same mod download (the author arranged them together)"] += 1
            return False
        ta, tb = targets[a], targets[b]
        if ta == tb:
            return True
        if loose and len(ta & tb) >= 2 and (ta <= tb or tb <= ta):
            return True
        reasons["they patch different sets of mods"] += 1
        return False

    pair_keys, hubs = defaultdict(list), 0
    verdict_cache = {}
    for key, names in holders.items():
        if len(names) < 2:
            continue
        if len(names) > HUB_LIMIT:
            hubs += 1
            continue
        names.sort(key=lambda x: order.get(x, 1 << 30))
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pr = (names[i], names[j])
                if pr not in verdict_cache:
                    verdict_cache[pr] = competing(*pr)
                if verdict_cache[pr]:
                    pair_keys[pr].append(key)
    if progress:
        progress("Looking up the original records...")
    wanted = {k for keys in pair_keys.values() for k in keys}
    cache = {}

    def buf(n):
        if n not in cache:
            cache[n] = Path(index[n.lower()]).read_bytes()
        return cache[n]
    ctx = {"edits": edits, "buf": buf, "base": BaseStore(index, wanted),
           "masters": {n: [m.lower() for m in plugins[n]["masters"]] for n in edits}}
    overlaps = []
    for k, ((a, b), keys) in enumerate(pair_keys.items()):   # a loads BEFORE b, so b wins shared records
        if progress and k % 20 == 0:
            progress("Checking overlaps... %d/%d" % (k, len(pair_keys)))
        try:
            conflicts, harmless, unverified = judge_pair(a, b, keys, ctx)
        except Exception:  # noqa: BLE001 - never let one odd record stop the whole scan
            conflicts, harmless, unverified = [], {}, len(keys)
        small = min(len(edits[a]), len(edits[b]))
        types = defaultdict(int)
        for key in keys:
            types[edits[a][key][0]] += 1
        verdict = "CONFLICT" if conflicts else ("UNVERIFIED" if unverified and not harmless else "HARMLESS")
        overlaps.append({"kind": "overlap", "first": a, "second": b, "shared": len(keys),
                         "size_first": len(edits[a]), "size_second": len(edits[b]),
                         "pct": round(len(keys) / small, 3) if small else 0,
                         "types": sorted(types.items(), key=lambda t: -t[1])[:4],
                         "verdict": verdict, "conflicts": conflicts[:25], "n_conflicts": len(conflicts),
                         "harmless": harmless, "unverified": unverified})
    order_v = {"CONFLICT": 0, "UNVERIFIED": 1, "HARMLESS": 2}
    overlaps.sort(key=lambda o: (order_v[o["verdict"]], -o["n_conflicts"], -o["shared"]))
    n_skipped = len([1 for v in verdict_cache.values() if not v])
    return overlaps, len(edits), hubs, skipped_big, n_skipped, dict(reasons)


NEXUS_SITES = {"skyrimse": "skyrimspecialedition", "skyrim": "skyrim", "skyrimvr": "skyrimspecialedition"}


def read_meta(mod_dir):
    """MO2 stores Nexus info for each mod in <mod folder>/meta.ini."""
    meta = {}
    f = Path(mod_dir) / "meta.ini"
    if not f.is_file():
        return meta
    in_general = False
    for line in read_text(f).splitlines():
        t = line.strip()
        if t.startswith("["):
            in_general = t.lower() == "[general]"
        elif in_general and "=" in t:
            k, v = t.split("=", 1)
            meta[k.strip().lower()] = v.strip()
    return meta


def describe_plugin(name, plugins, index, mods_dir):
    """Facts that help a human decide which of two patches to keep."""
    import datetime
    lines = []
    path = index.get(name.lower())
    h = plugins.get(name, {})
    if path is not None:
        try:
            rel = Path(path).relative_to(mods_dir).parts[0] if mods_dir else None
        except ValueError:
            rel = None
        if rel:
            meta = read_meta(Path(mods_dir) / rel)
            ver = meta.get("version")
            lines.append("MO2 mod:    %s%s" % (rel, "  (version %s)" % ver if ver else ""))
            mid = meta.get("modid", "0")
            if mid.lstrip("-").isdigit() and int(mid) > 0:
                site = NEXUS_SITES.get(meta.get("gamename", "skyrimse").lower(), "skyrimspecialedition")
                lines.append("Nexus page: https://www.nexusmods.com/%s/mods/%s" % (site, mid))
        try:
            st = Path(path).stat()
            lines.append("File date:  %s    Size: %d KB" % (
                datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"), st.st_size // 1024))
        except OSError:
            pass
    if h.get("author"):
        lines.append("Author tag: " + h["author"])
    if h.get("description"):
        lines.append("Description: " + h["description"][:90].replace("\n", " "))
    return lines


def newest_hint(members, index):
    best, best_t = None, 0
    for m in members:
        try:
            t = Path(index[m.lower()]).stat().st_mtime
        except (KeyError, OSError):
            continue
        if t > best_t:
            best, best_t = m, t
    return best


def flist(codes):
    return ", ".join(FIELD_NAMES.get(c, c) for c in codes)


def format_overlaps(overlaps, stats, show_all=False, limit=40):
    scanned, hubs, skipped, ignored = stats[0], stats[1], stats[2], stats[3]
    conf = [o for o in overlaps if o["verdict"] == "CONFLICT"]
    other = [o for o in overlaps if o["verdict"] != "CONFLICT"]
    out = ["== COMPETING PATCHES THAT CLASH (%d) ==" % len(conf),
           "(Looked at patches that do the SAME JOB for the SAME mods, made separately. A clash is when",
           " both change the same thing in the game differently, so only one can work.)", ""]
    if not conf:
        out.append("None found. Where your patches overlap, the later one keeps everything the earlier")
        out.append("one changed.")
        out.append("")
    for o in conf[:limit]:
        out.append("[CONFLICT] these two patches change the same thing differently (%d record(s))" % o["n_conflicts"])
        out.append("  * %s   (loads first, LOSES)" % o["first"])
        out.append("  * %s   (loads later, WINS)" % o["second"])
        for c in o["conflicts"][:4]:
            out.append("    - %s %s (from %s) both change: %s" % (c["record"], c["formid"], c["master"], flist(c["lost"])))
        if o["n_conflicts"] > 4:
            out.append("    - ... and %d more record(s)" % (o["n_conflicts"] - 4))
        out.append("  -> You almost certainly need only ONE of these. Read both Nexus pages to see which")
        out.append("     one the authors recommend, disable the other and test.")
        out.append("")
    if len(conf) > limit:
        out.append("... and %d more (see patch_report.html or use --json)." % (len(conf) - limit))
        out.append("")
    if ignored:
        out.append("(%d pair(s) of patches that touch the same records were ignored on purpose: one is built on the" % ignored)
        out.append(" other, they come from the same download, or they patch different mods. That is normal.)")
    harmless = [o for o in other if o["verdict"] == "HARMLESS"]
    unver = [o for o in other if o["verdict"] == "UNVERIFIED"]
    out.append("(%d competing pair(s) checked and found compatible. Hidden.)" % len(harmless))
    if unver:
        out.append("(%d overlap(s) could not be checked because the original record was not found.)" % len(unver))
    if show_all:
        for o in other:
            out.append("   %s: %s + %s" % (o["verdict"].lower(), o["first"], o["second"]))
    if hubs:
        out.append("(%d record(s) edited by more than %d patches were ignored as noise.)" % (hubs, HUB_LIMIT))
    out.append("")
    return out


def format_report(findings, total, unreadable, hidden_weak=0, plugins=None, index=None, mods_dir=None,
                  overlaps=None, ov_stats=None, show_minor=False):
    plugins, index = plugins or {}, index or {}
    dups = [f for f in findings if f["kind"] == "duplicate_patch"]
    miss = [f for f in findings if f["kind"] == "missing_master"]
    out = ["Redundant Patch Finder %s" % VERSION, "Scanned %d active plugins." % total, "",
           "SUMMARY: %d same-purpose patch group(s), %d real patch conflict(s), %d missing master(s)."
           % (len(dups), len([o for o in (overlaps or []) if o["verdict"] == "CONFLICT"]), len(miss)), ""]
    out.append("== PATCHES WITH THE SAME PURPOSE (by name and requirements) (%d) ==" % len(dups))
    if not dups:
        out.append("None found.")
    order = {"HIGH": 0, "NAME": 1, "MEDIUM": 2}
    for f in sorted(dups, key=lambda x: order.get(x["confidence"], 9)):
        label = {"HIGH": "LIKELY DUPLICATES", "NAME": "SAME NAME, DIFFERENT REQUIREMENTS",
                 "MEDIUM": "WEAK MATCH"}[f["confidence"]]
        out.append("[%s]" % label)
        for p in f["plugins"]:
            out.append("  * " + p)
            out += ["      " + l for l in describe_plugin(p, plugins, index, mods_dir)]
        out.append("  Related mods: " + ", ".join(f["requires"]))
        if f["confidence"] == "HIGH":
            out.append("  These look like the same patch made twice (names and requirements match).")
        elif f["confidence"] == "NAME":
            out.append("  Their names match but they require different mods. They may be two authors' "
                       "versions of the same patch, or different patches.")
        else:
            out.append("  Different names. Probably different patches.")
        if f["confidence"] in ("HIGH", "NAME"):
            nw = newest_hint(f["plugins"], index)
            out.append("  WHAT TO DO: open each mod's Nexus page (links above when known), read the")
            out.append("  description and changelog, and check which one is still maintained or says")
            out.append("  it replaces the other.")
            out.append("  Then disable ONE and test. Always keep a backup of your save.")
            if nw:
                out.append("  Hint only: the most recently dated file is '%s'. Newer is NOT always "
                           "better, so check the pages." % nw)
        out.append("")
    if hidden_weak:
        out.append("(%d weak match(es) hidden: plugins that need the same mods but have different "
                   "names. Run with --aggressive to show them.)" % hidden_weak)
        out.append("")
    if overlaps is not None:
        out += format_overlaps(overlaps, ov_stats, show_minor)
    out.append("== MISSING MASTERS (%d) ==" % len(miss))
    if not miss:
        out.append("None found.")
    for f in miss:
        out.append("[!] %s needs %s  (%s)" % (f["plugins"][0], f["master"], f["reason"]))
    if unreadable:
        out += ["", "== COULD NOT READ (%d) ==" % len(unreadable)]
        out += ["    - %s: %s" % (n, why) for n, why in unreadable]
    out += ["", "This tool only suggests candidates. It never changes your files."]
    return "\n".join(out)


# --------------------------------------------------------------------------- main
def run(args):
    mode_note = ""
    mods_dir = None   # only MO2 has per-mod folders
    cc_check = False  # only check Creation Club files when we know where the game's Data folder is
    if args.data or not (args.instance or find_instances()):
        # ---- Vortex / Wrye Bash / manual install: plugins live in the real Data folder
        if args.data:
            data_dir = Path(args.data)
        else:
            cands = game_data_candidates()
            if not cands:
                raise SystemExit("Could not find your Skyrim Data folder automatically.\n"
                                 "Run again with:  --data \"D:\\Games\\Skyrim Special Edition\\Data\"")
            data_dir = cands[0]
        if not data_dir.is_dir():
            raise SystemExit("Data folder not found: %s" % data_dir)
        index = build_flat_index(data_dir)
        load_ccc(data_dir)
        cc_check = True
        txt = Path(args.plugins_txt) if args.plugins_txt else None
        if txt is None:
            cands = plugins_txt_candidates()
            txt = cands[0] if cands else None
        if txt and txt.is_file():
            active, inactive = parse_plugins_txt(txt)
            mode_note = "Data: %s | Load order: %s" % (data_dir, txt)
        else:
            active, inactive = sorted(f.name for f in index.values()), []
            mode_note = ("Data: %s | No plugins.txt found, so ALL plugins in the folder are "
                         "treated as active." % data_dir)
    else:
        # ---- Mod Organizer 2: plugins live in a virtual folder
        if args.instance:
            instance = Path(args.instance)
        else:
            inst = find_instances()
            if len(inst) > 1:
                raise SystemExit("Several MO2 instances found, pick one with --instance:\n  "
                                 + "\n  ".join(map(str, inst)))
            instance = inst[0]
        if not (instance / "ModOrganizer.ini").is_file():
            raise SystemExit("No ModOrganizer.ini in %s" % instance)
        base, sel_profile, game_data = locate_dirs(instance)
        if args.game_data:
            game_data = Path(args.game_data)
        profile = args.profile or sel_profile
        profile_dir = base / "profiles" / profile
        if not profile_dir.is_dir():
            raise SystemExit("Profile folder not found: %s" % profile_dir)
        active, inactive = load_active_plugins(profile_dir)
        index = build_file_index(base, profile_dir, game_data)
        mods_dir = base / "mods"
        if game_data and Path(game_data).is_dir():
            load_ccc(game_data)
            cc_check = True
        mode_note = "MO2 instance: %s | Profile: %s" % (instance, profile)

    print(mode_note, file=sys.stderr)
    plugins, unreadable = {}, []
    for name in active:
        path = index.get(name.lower())
        if path is None:
            if not is_vanilla(name):
                unreadable.append((name, "file not found"))
            continue
        try:
            plugins[name] = read_header(path)
        except Exception as e:  # noqa: BLE001 - report and keep going
            unreadable.append((name, str(e)))

    overlaps = ov_stats = None
    if not args.no_scan:
        def mod_of(n):
            try:
                return Path(index[n.lower()]).relative_to(mods_dir).parts[0] if mods_dir else None
            except (ValueError, KeyError):
                return None
        overlaps, n_scanned, hubs, big, ignored, why = analyse_overlaps(
            plugins, active, index, args.min_masters,
            progress=lambda m: print(m, file=sys.stderr, end="\r"), mod_of=mod_of, loose=args.loose)
        print(" " * 50, file=sys.stderr, end="\r")
        ov_stats = (n_scanned, hubs, big, ignored, why)
    findings = analyse(plugins, active, inactive, args.min_masters, frozenset(index), cc_check)
    hidden = 0
    if not args.aggressive:  # weak matches are mostly noise on big modlists
        hidden = sum(1 for f in findings if f["confidence"] == "MEDIUM")
        findings = [f for f in findings if f["confidence"] != "MEDIUM"]
    text = json.dumps({"findings": findings, "scanned": len(plugins),
                       "overlaps": overlaps, "unreadable": unreadable}, indent=2) if args.json \
        else format_report(findings, len(plugins), unreadable, hidden, plugins, index, mods_dir,
                           overlaps, ov_stats, args.aggressive)
    print(colorize(text) if (not args.json and use_color(args.no_color)) else text)
    if args.html and not args.json:
        Path(args.html).write_text(
            build_html(findings, overlaps, ov_stats, len(plugins), unreadable, plugins, index, mods_dir,
                       args.aggressive), encoding="utf-8")
        print("Visual report saved to %s" % args.html, file=sys.stderr)
        if getattr(args, "open_browser", False):
            import webbrowser
            webbrowser.open(Path(args.html).resolve().as_uri())
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print("\nReport saved to %s" % args.out, file=sys.stderr)


# --------------------------------------------------------------------------- console colours
C = {"reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m", "red": "\033[91m", "green": "\033[92m",
     "yellow": "\033[93m", "cyan": "\033[96m", "magenta": "\033[95m"}


def use_color(force_off=False):
    if force_off or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # switches on ANSI colour support in the Windows 10/11 console
    return True


def colorize(text):
    out = []
    for line in text.splitlines():
        t = line.strip()
        if line.startswith("=="):
            line = C["bold"] + C["cyan"] + line + C["reset"]
        elif t.startswith(("[CONFLICT", "[LIKELY")):
            line = C["bold"] + C["red"] + line + C["reset"]
        elif t.startswith(("[SAME NAME", "[!]")):
            line = C["yellow"] + line + C["reset"]
        elif t.startswith(("None found", "SUMMARY")):
            line = C["green"] + line + C["reset"]
        elif t.startswith(("->", "WHAT TO DO", "Hint only", "(")):
            line = C["dim"] + line + C["reset"]
        elif "(loads first, LOSES)" in line:
            line = C["red"] + line + C["reset"]
        elif "(loads later, WINS)" in line:
            line = C["green"] + line + C["reset"]
        out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------- HTML report
def build_html(findings, overlaps, ov_stats, total, unreadable, plugins, index, mods_dir, show_minor):
    from html import escape as e
    dups = [f for f in findings if f["kind"] == "duplicate_patch" and f["confidence"] != "MEDIUM"]
    miss = [f for f in findings if f["kind"] == "missing_master"]
    ovs = [o for o in (overlaps or []) if o["verdict"] == "CONFLICT"]
    quiet = [o for o in (overlaps or []) if o["verdict"] != "CONFLICT"]
    pairs_with_overlap = {frozenset((o["first"], o["second"])) for o in ovs}
    same_purpose = {frozenset(f["plugins"]) for f in dups}
    # a name-based match already explained by a content overlap is merged into that card
    dups = [f for f in dups if not any(set(f["plugins"]) <= set(pr) for pr in pairs_with_overlap)]
    strong = len(ovs)

    def plug_block(name, role=""):
        det = "".join("<div>%s</div>" % (
            ('<a href="%s" target="_blank">%s</a>' % (e(l.split(": ", 1)[1].strip()), e(l)))
            if l.startswith("Nexus page") else e(l)) for l in describe_plugin(name, plugins, index, mods_dir))
        return '<div class="pl"><b>%s</b> %s<div class="det">%s</div></div>' % (e(name), role, det)

    cards, HTML_CAP = [], 150
    for o in ovs[:HTML_CAP]:
        lines = "".join(
            '<li><b>%s</b> %s <span class="muted">(from %s)</span> both change: <b>%s</b></li>' % (
                e(c["record"]), e(c["formid"]), e(c["master"]), e(flist(c["lost"]))) for c in o["conflicts"][:6])
        more = (" <li>... and %d more records</li>" % (o["n_conflicts"] - 6)) if o["n_conflicts"] > 6 else ""
        note = ("These do the same job for the same mods, and both change the same things in the game, so only one "
                "can win. You most likely need just ONE of them. Check both Nexus pages for the author's recommendation, "
                "then disable the other and test.")
        if frozenset((o["first"], o["second"])) in same_purpose:
            note += " Their names and requirements also match, so they were probably meant to do the same job."
        cards.append(
            '<div class="card strong"><div class="hd"><span class="badge strong">COMPETING PATCHES</span> '
            'both change the same thing differently (%d record(s))</div>%s%s<ul class="lost">%s%s</ul>'
            '<p class="note">%s</p></div>' % (
                o["n_conflicts"],
                plug_block(o["first"], '<span class="tag lose">loads first &middot; loses</span>'),
                plug_block(o["second"], '<span class="tag win">loads later &middot; wins</span>'), lines, more, note))
    if len(ovs) > HTML_CAP:
        cards.append('<p class="muted">Showing the %d worst of %d conflicts. Use --json for the full list.</p>'
                     % (HTML_CAP, len(ovs)))
    for f in dups:
        lab = {"HIGH": "LIKELY DUPLICATES", "NAME": "SAME NAME, DIFFERENT REQUIREMENTS"}[f["confidence"]]
        nw = newest_hint(f["plugins"], index)
        cards.append('<div class="card %s"><div class="hd"><span class="badge %s">%s</span></div>%s'
                     '<p class="note">Open each mod\'s Nexus page, read the description and changelog, disable ONE and test.'
                     '%s</p></div>' % (
                         "strong" if f["confidence"] == "HIGH" else "partial",
                         "strong" if f["confidence"] == "HIGH" else "partial", lab,
                         "".join(plug_block(x) for x in f["plugins"]),
                         (" Hint only: the most recently dated file is <b>%s</b> (newer is not always better)." % e(nw)) if nw else ""))
    mrows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (e(f["plugins"][0]), e(f["master"]), e(f["reason"]))
                    for f in miss) or '<tr><td colspan="3" class="ok">None found</td></tr>'
    nscan = ov_stats[0] if ov_stats else 0
    n_ok = len([o for o in quiet if o["verdict"] == "HARMLESS"])
    n_unv = len(quiet) - n_ok
    qrows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        e(o["first"]), e(o["second"]),
        e("; ".join(sorted(o["harmless"])) if o["harmless"] else "original record not found, could not check"))
        for o in quiet)
    quiet_html = ('<h2>Compared and found compatible (%d)</h2><div class="muted">These patches do the same job, but do not clash. No action needed. '
                  'Rows marked "could not check" had no original record to compare against.</div>'
                  '<details><summary>Show the list</summary><table><tr><td><b>Loads first</b></td><td><b>Loads later</b>'
                  '</td><td><b>Why it is fine</b></td></tr>%s</table></details>' % (len(quiet), qrows)) if quiet else ""
    return """<!doctype html><html><head><meta charset="utf-8"><title>Redundant Patch Finder</title><style>
body{background:#14161a;color:#e6e6e6;font:15px/1.5 Segoe UI,system-ui,sans-serif;margin:0;padding:24px;max-width:1000px;margin:auto}
h1{margin:0 0 4px}h2{margin-top:32px;border-bottom:1px solid #333;padding-bottom:6px}.muted{color:#8a8f98;font-size:13px}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}.tile{flex:1;min-width:150px;background:#1e2127;border-radius:10px;padding:14px;border-left:5px solid #4caf50}
.tile b{font-size:28px;display:block}.tile.red{border-color:#ef5350}.tile.amber{border-color:#ffb300}
.card{background:#1e2127;border-radius:10px;padding:14px 16px;margin:12px 0;border-left:5px solid #666}
ul.lost{margin:10px 0 0 4px;font-size:13px;color:#c9ccd1}details{margin-top:8px}summary{cursor:pointer;color:#64b5f6}
.card.strong{border-color:#ef5350}.card.partial{border-color:#ffb300}.card.minor{border-color:#607d8b}
.badge{font-weight:700;font-size:12px;padding:3px 8px;border-radius:5px;color:#111;margin-right:6px}
.badge.strong{background:#ef5350}.badge.partial{background:#ffb300}.badge.minor{background:#90a4ae}
.pl{margin:8px 0 0 6px;padding-left:10px;border-left:2px solid #333}.det{font-size:13px;color:#9aa0a8}
.tag{font-size:12px;padding:1px 7px;border-radius:9px;margin-left:6px}.tag.lose{background:#4a2a2a;color:#ff8a80}.tag.win{background:#23402a;color:#a5d6a7}
.note{color:#b0b5bd;margin:10px 0 0}a{color:#64b5f6}table{width:100%%;border-collapse:collapse}td{padding:6px 8px;border-bottom:1px solid #2a2d33;font-size:14px}
.ok{color:#81c784}input{width:100%%;padding:10px;border-radius:8px;border:1px solid #333;background:#1e2127;color:#eee;font-size:15px}
</style></head><body>
<h1>Redundant Patch Finder <span class="muted">v%s</span></h1>
<div class="muted">Scanned %d active plugins, compared the contents of %d patches. This tool only suggests candidates and never changes your files.</div>
<div class="tiles"><div class="tile %s"><b>%d</b>competing patches / duplicates</div><div class="tile"><b>%d</b>compatible pairs (hidden)</div>
<div class="tile %s"><b>%d</b>missing masters</div></div>
<h2>Patches that need your attention</h2><input id="q" placeholder="Filter by plugin name..." oninput="f()">
<div id="cards">%s</div>%s
<h2>Missing masters</h2><table>%s</table>
<script>function f(){var q=document.getElementById('q').value.toLowerCase();document.querySelectorAll('#cards .card').forEach(function(c){c.style.display=c.textContent.toLowerCase().indexOf(q)<0?'none':''})}</script>
</body></html>""" % (VERSION, total, nscan,
        "red" if (strong + len(dups)) else "", strong + len(dups),
        n_ok,
        "amber" if miss else "", len(miss),
        "".join(cards) or '<p class="ok">No real conflicts found. Where patches overlap, the later one keeps the earlier one\'s changes.</p>',
        quiet_html, mrows)


def pause():
    if os.environ.get("PATCH_FINDER_NO_PAUSE"):  # the .bat launcher does its own pause
        return
    try:
        input("\nPress Enter to close...")
    except EOFError:  # no keyboard attached (scripts, CI)
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Find redundant patches and missing masters. Works with Mod Organizer 2, "
                    "Vortex, Wrye Bash, or no mod manager at all.")
    ap.add_argument("--instance", help="MO2 instance folder (contains ModOrganizer.ini)")
    ap.add_argument("--profile", help="MO2 profile name (default: the one MO2 has selected)")
    ap.add_argument("--game-data", help="MO2 only: Skyrim Data folder (auto-detected if omitted)")
    ap.add_argument("--data", help="Vortex / manual / Wrye Bash: your Skyrim 'Data' folder")
    ap.add_argument("--plugins-txt", help="Path to plugins.txt (auto-detected if omitted)")
    ap.add_argument("--min-masters", type=int, default=2,
                    help="Only treat plugins with at least this many non-vanilla masters as "
                         "patches (default 2)")
    ap.add_argument("--aggressive", action="store_true",
                    help="Also show weak matches (same required mods, different names). "
                         "Noisy on big modlists.")
    ap.add_argument("--loose", action="store_true",
                    help="Also compare patches whose list of mods only partly matches (more results, more noise)")
    ap.add_argument("--no-scan", action="store_true",
                    help="Skip reading patch contents (faster, names/requirements only)")
    ap.add_argument("--html", help="Also write a colourful HTML report to this file")
    ap.add_argument("--no-browser", action="store_true", help="Do not open the HTML report automatically")
    ap.add_argument("--no-color", action="store_true", help="Plain text, no colours")
    ap.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    ap.add_argument("--out", help="Also write the report to this file")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # odd plugin names must never crash the printout
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    double_clicked = argv is None and len(sys.argv) == 1  # no arguments given
    if double_clicked and not args.out:
        args.out = str(Path.cwd() / "patch_report.txt")
    if double_clicked and not args.html:
        args.html = str(Path.cwd() / "patch_report.html")
    args.open_browser = bool(args.html) and double_clicked and not args.no_browser
    try:
        run(args)
    except SystemExit as e:
        if double_clicked and e.code not in (None, 0):
            print("\n" + str(e.code))
            pause()
            raise SystemExit(1)  # message already shown
        raise
    except Exception:  # noqa: BLE001 - show the problem instead of closing silently
        import traceback
        tb = traceback.format_exc()
        try:
            Path.cwd().joinpath("patch_finder_error.txt").write_text(tb, encoding="utf-8")
        except OSError:
            pass
        print("\nSomething went wrong. Please copy ALL of the text below and send it to the "
              "developer:\n\n" + tb)
        if double_clicked:
            pause()
        raise SystemExit(1)
    if double_clicked:
        pause()
    return 0


if __name__ == "__main__":
    sys.exit(main())
