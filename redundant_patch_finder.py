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
import math
import mmap
import zlib
import os
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

VERSION = "0.3.0"
NEW_ABS, NEW_FRAC = 20, 0.15   # a plugin adding more than this much of its own content is a mod/addon, not a patch
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
    n_masters, out, end, n_new = len(masters), {}, len(data), 0
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
        elif idx >= n_masters:
            n_new += 1                                  # a record this plugin creates itself
        pos += 24 + size
    return out, n_new


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


POS_TYPES = {"REFR", "ACHR", "ACRE", "PGRE", "PMIS", "PHZD", "PARW", "PBAR", "PBEA", "PCON", "PFLA"}
PATCH_WORDS = re.compile(r"(?<![A-Za-z])(?:patch|fix|compat|integration|resolution)", re.I)


def classify(name, folder, n_new, size_bytes, n_nonvanilla):
    """A PATCH fixes how mods work together. A MOD is a base (Lux Via, Lux Orbis...)."""
    def words(t):
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t or "")     # MusicPatch -> Music Patch
    if PATCH_WORDS.search(words(name)) or PATCH_WORDS.search(words(folder)):
        return "patch"
    if n_new == 0 and size_bytes < 60000 and n_nonvanilla >= 2:   # tiny and creates nothing: a patch
        return "patch"
    return "mod"


def pos_info(rtype, ra, rb, rbase):
    """For placed objects: how far did each plugin move it from the original (game units)?"""
    if rtype not in POS_TYPES:
        return None
    try:
        a, b, c = (struct.unpack("<6f", r["DATA"][0]) for r in (ra, rb, rbase))
    except (KeyError, IndexError, struct.error):
        return None
    d = lambda p, q: math.sqrt(sum((x - y) ** 2 for x, y in zip(p[:3], q[:3])))
    rot = max(abs(x - y) for x, y in zip(a[3:], b[3:]))
    gap = d(a, b)
    return {"first_move": round(d(c, a), 1), "second_move": round(d(c, b), 1), "gap": round(gap, 1),
            "dz": round(b[2] - a[2], 1), "near": gap <= 2.0 and rot <= 0.05}


def judge_pair(a, b, keys, ctx):
    """Compare what the EARLIER plugin a and the LATER plugin b do to the shared records.
    clash  = both changed the same field (vs the original) to different values
    revert = the later one puts a field back to the original, undoing the earlier one"""
    conflicts, reverts, harmless, unverified = [], [], defaultdict(int), 0
    ma, mb = ctx["masters"][a], ctx["masters"][b]
    for key in keys:
        ra = parse_record(ctx["buf"](a), ctx["edits"][a][key][1])
        rb = parse_record(ctx["buf"](b), ctx["edits"][b][key][1])
        rtype = ctx["edits"][a][key][0]
        types = set(ra) | set(rb)
        if all(lists_same(ra.get(t, []), rb.get(t, []), ma, a.lower(), mb, b.lower()) for t in types):
            harmless["both plugins contain identical data"] += 1
            continue
        base = ctx["base"](key)
        if base is None:
            unverified += 1
            continue
        rbase, mbase, nbase = base
        pos = pos_info(rtype, ra, rb, rbase)
        clash, rev = [], []
        for t in types:
            if lists_same(ra.get(t, []), rbase.get(t, []), ma, a.lower(), mbase, nbase):
                continue                              # the earlier plugin did not change this field
            if lists_same(ra.get(t, []), rb.get(t, []), ma, a.lower(), mb, b.lower()):
                continue
            if t == "DATA" and pos and pos["near"]:
                continue                              # positions within 2 units: the same in practice
            if lists_same(rb.get(t, []), rbase.get(t, []), mb, b.lower(), mbase, nbase):
                rev.append(t)
            else:
                clash.append(t)
        item = {"record": rtype, "formid": "%06X" % key[1], "master": key[0]}
        if clash:
            conflicts.append(dict(item, lost=sorted(clash), reverts=sorted(rev),
                                  pos=pos if "DATA" in clash + rev else None))
        elif rev:
            reverts.append(dict(item, lost=sorted(rev), pos=pos if "DATA" in rev else None))
        elif pos and pos["near"]:
            harmless["the two plugins already agree (positions within 2 units)"] += 1
        elif all(lists_same(ra.get(t, []), rbase.get(t, []), ma, a.lower(), mbase, nbase) for t in types):
            harmless["the first plugin changes nothing compared with the original"] += 1
        else:
            harmless["they change different things, or the later one carries the changes forward"] += 1
    return conflicts, reverts, dict(harmless), unverified


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
                     mod_of=None, loose=False, info=None):
    """Find plugins that edit the same records and judge each pair.
    RED    = two PATCHES for the same mods that clash (rival fixes)
    YELLOW = anything else that overrides the same thing differently, or a fix that may be obsolete
    GREEN  = overlap with no problem.  Returns (overlaps, scanned, hubs, skipped_big, 0, {})."""
    order = {n: i for i, n in enumerate(active)}
    cands = [n for n, h in plugins.items()
             if len([m for m in h["masters"] if not is_vanilla(m)]) >= min_masters]
    edits, skipped_big, kind = {}, 0, {}
    for k, n in enumerate(cands):
        h = plugins[n]
        if h.get("num_records") and h["num_records"] > max_records:
            skipped_big += 1
            continue
        try:
            e, n_new = scan_overrides(index[n.lower()], h["masters"])
            size = Path(index[n.lower()]).stat().st_size
        except Exception:  # noqa: BLE001
            continue
        if e:
            edits[n] = e
            kind[n] = classify(n, mod_of(n) if mod_of else None, n_new, size,
                               len([m for m in h["masters"] if not is_vanilla(m)]))
        if progress and k % 25 == 0:
            progress("Reading patch contents... %d/%d" % (k, len(cands)))
    holders = defaultdict(list)
    for n, e in edits.items():
        for key in e:
            holders[key].append(n)
    targets = {n: frozenset(m.lower() for m in plugins[n]["masters"] if not is_vanilla(m)) for n in edits}
    rel_cache = {}

    def relation(a, b):
        """(are they RIVAL patches?, label explaining how they relate)"""
        if a.lower() in targets[b] or b.lower() in targets[a]:
            return False, "one is built on the other"
        if mod_of and mod_of(a) and mod_of(a) == mod_of(b):
            return False, "same download (arranged together by one author)"
        ka, kb = kind[a], kind[b]
        if ka != kb:
            return False, "a patch applied on top of a mod"
        if ka == "mod":
            return False, "two mods editing the same thing"
        ta, tb = targets[a], targets[b]
        if ta == tb or (loose and len(ta & tb) >= 2 and (ta <= tb or tb <= ta)):
            return True, "rival patches for the same mods"
        if ta < tb or tb < ta:
            return False, "one patch covers the same mods plus more (it may replace the smaller one)"
        return False, "patches for different mods"

    pair_keys, hubs = defaultdict(list), 0
    for key, names in holders.items():
        if len(names) < 2:
            continue
        if len(names) > HUB_LIMIT:
            hubs += 1
            continue
        names.sort(key=lambda x: order.get(x, 1 << 30))
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pair_keys[(names[i], names[j])].append(key)
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

    def make_hints(a, b, conflicts, reverts, harmless, n_shared):
        hints, ka, kb = [], kind[a], kind[b]
        obsolete = False
        if ka != kb:
            P, M = (a, b) if ka == "patch" else (b, a)
            if order.get(P, 0) < order.get(M, 0):
                hints.append("The mod loads AFTER the patch, so the mod overrides the patch: the patch has no "
                             "effect on these records. Check your load order.")
            else:
                only_agree = (n_shared == len(edits[P]) and not conflicts and not reverts and harmless and
                              set(harmless) <= {"the two plugins already agree (positions within 2 units)",
                                                "both plugins contain identical data"})
                if only_agree:
                    obsolete = True
                    hints.append("The mod already contains what this patch does (same values). The patch looks "
                                 "UNNECESSARY or outdated. Check the mod's changelog, then try disabling the patch.")
                elif info:
                    im, ip = info(M), info(P)
                    if im and ip and im.get("date") and ip.get("date") and (im["date"] - ip["date"]).days >= 30:
                        hints.append("The mod (%s, %s) is NEWER than this patch (%s, %s). The mod may already "
                                     "include its own correction. Check the mod's changelog and the patch page "
                                     "for a note that it is still needed." % (
                                         im.get("version") or "?", im["date"], ip.get("version") or "?", ip["date"]))
                    elif im and ip and im.get("date") and ip.get("date"):
                        hints.append("The patch is as new as, or newer than, the mod (%s vs %s), so it was "
                                     "probably made for this version of the mod." % (ip["date"], im["date"]))
                    hints.append("The patch still changes things the mod does not, so it is probably still doing its job.")
        elif ka == "patch" and (conflicts or reverts):
            hints.append("Two patches changing the same thing: you usually need only ONE. Read both Nexus pages.")
        return hints, obsolete

    overlaps = []
    for k, ((a, b), keys) in enumerate(pair_keys.items()):       # a loads BEFORE b, so b wins shared records
        if progress and k % 20 == 0:
            progress("Checking overlaps... %d/%d" % (k, len(pair_keys)))
        if (a, b) not in rel_cache:
            rel_cache[(a, b)] = relation(a, b)
        rival, why = rel_cache[(a, b)]
        try:
            conflicts, reverts, harmless, unverified = judge_pair(a, b, keys, ctx)
        except Exception:  # noqa: BLE001 - never let one odd record stop the whole scan
            conflicts, reverts, harmless, unverified = [], [], {}, len(keys)
        hints, obsolete = make_hints(a, b, conflicts, reverts, harmless, len(keys))
        # A later patch that edits the same records may already tie the two together (a "bridge" patch).
        resolvers = set()
        if conflicts or reverts:
            resolvers, covered_all = set(), True
            for it in conflicts + reverts:
                key = (it["master"], int(it["formid"], 16))
                cover = {c for c in holders.get(key, []) if c not in (a, b) and kind[c] == "patch"
                         and order.get(c, 0) > order.get(b, 0)}
                if not cover:
                    covered_all = False
                    break
                resolvers |= cover
            if not covered_all:
                resolvers = set()
        if resolvers:
            names = sorted(resolvers, key=lambda x: order.get(x, 0))
            hints.append("RESOLVED: a later patch also edits these records and loads after both, so it probably "
                         "ties them together: " + ", ".join(names[:3]) + ". Nothing to do if it is made for both.")
            verdict = "HARMLESS"
        elif conflicts and rival:
            verdict = "CONFLICT"
        elif obsolete or ((conflicts or reverts) and kind[a] == "patch" and kind[b] == "patch"
                          and why.startswith(("patches for different", "rival patches", "one patch covers"))):
            verdict = "OVERLAP"    # yellow: only patch-vs-patch clashes, or a patch the mod made useless
        elif unverified and not harmless:
            verdict = "UNVERIFIED"
        else:
            verdict = "HARMLESS"   # includes mod-vs-mod and patch-on-mod: normal, not worth reading
        types = defaultdict(int)
        for key in keys:
            types[edits[a][key][0]] += 1
        small = min(len(edits[a]), len(edits[b]))
        overlaps.append({"kind": "overlap", "first": a, "second": b, "shared": len(keys),
                         "size_first": len(edits[a]), "size_second": len(edits[b]),
                         "pct": round(len(keys) / small, 3) if small else 0,
                         "types": sorted(types.items(), key=lambda t: -t[1])[:4],
                         "verdict": verdict, "relation": why, "rival": rival,
                         "kinds": (kind[a], kind[b]), "obsolete": obsolete, "hints": hints,
                         "conflicts": conflicts[:25], "n_conflicts": len(conflicts),
                         "reverts": reverts[:25], "n_reverts": len(reverts),
                         "harmless": harmless, "unverified": unverified})
    rank = {"CONFLICT": 0, "OVERLAP": 1, "UNVERIFIED": 2, "HARMLESS": 3}
    overlaps.sort(key=lambda o: (rank[o["verdict"]], 0 if "patch" in o["kinds"] else 1,
                                 -(o["n_conflicts"] + o["n_reverts"]), -o["shared"]))
    return overlaps, len(edits), hubs, skipped_big, 0, kind


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


def pos_text(pos):
    if not pos:
        return ""
    return ("first plugin moves it %.0f units from the original, the second moves it %.0f; they end %.0f units "
            "apart (height difference %+.0f)" % (pos["first_move"], pos["second_move"], pos["gap"], pos["dz"]))


def format_overlaps(overlaps, stats, show_all=False, limit=30):
    red = [o for o in overlaps if o["verdict"] == "CONFLICT"]
    yellow = [o for o in overlaps if o["verdict"] == "OVERLAP"]
    green = [o for o in overlaps if o["verdict"] in ("HARMLESS", "UNVERIFIED")]
    scanned, hubs = stats[0], stats[1]
    out = ["== RED: RIVAL PATCHES THAT CLASH (%d) ==" % len(red),
           "(Two PATCHES for the SAME mods, made separately, that change the same thing differently.", " Only one can work.)", ""]
    if not red:
        out += ["None found.", ""]
    for o in red[:limit]:
        out.append("[CONFLICT] %d record(s)" % o["n_conflicts"])
        out.append("  * %s   (PATCH, loads first, LOSES)" % o["first"])
        out.append("  * %s   (PATCH, loads later, WINS)" % o["second"])
        for c in o["conflicts"][:4]:
            out.append("    - %s %s (from %s) both change: %s" % (c["record"], c["formid"], c["master"], flist(c["lost"])))
        out.append("  -> You almost certainly need only ONE of these. Read both Nexus pages, disable one, test.")
        out.append("")
    out += ["== YELLOW: PATCHES EDITING THE SAME THING (%d) ==" % len(yellow),
            "(Not rival patches, but they change the same thing differently, or a patch looks obsolete.", " Worth a look, often fine.)", ""]
    if not yellow:
        out += ["None found.", ""]
    for o in yellow[:limit]:
        tag = "POSSIBLY OBSOLETE PATCH" if o["obsolete"] else o["relation"].upper()
        out.append("[%s]" % tag)
        out.append("  * %s   (%s, loads first)" % (o["first"], o["kinds"][0].upper()))
        out.append("  * %s   (%s, loads later, WINS)" % (o["second"], o["kinds"][1].upper()))
        for c in (o["conflicts"] + o["reverts"])[:3]:
            verb = "both change" if c in o["conflicts"] else "later one undoes the earlier one's change to"
            out.append("    - %s %s (from %s) %s: %s" % (c["record"], c["formid"], c["master"], verb, flist(c["lost"])))
            if c.get("pos"):
                out.append("      " + pos_text(c["pos"]))
        for h in o["hints"]:
            out.append("  -> " + h)
        out.append("")
    if len(yellow) > limit:
        out += ["... and %d more yellow pair(s) (see patch_report.html)." % (len(yellow) - limit), ""]
    out.append("== GREEN: %d overlap(s) checked, no problem found ==" % len(green))
    if show_all:
        for o in green:
            out.append("   %s + %s" % (o["first"], o["second"]))
    if hubs:
        out.append("(%d record(s) edited by more than %d plugins were ignored as noise.)" % (hubs, HUB_LIMIT))
    out.append("")
    return out


def format_report(findings, total, unreadable, hidden_weak=0, plugins=None, index=None, mods_dir=None,
                  overlaps=None, ov_stats=None, show_minor=False):
    plugins, index = plugins or {}, index or {}
    dups = [f for f in findings if f["kind"] == "duplicate_patch"]
    miss = [f for f in findings if f["kind"] == "missing_master"]
    out = ["Redundant Patch Finder %s" % VERSION, "Scanned %d active plugins." % total, "",
           "SUMMARY: %d same-name patch group(s), %d red conflict(s), %d missing master(s)."
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
        def plugin_info(n):
            """Version (from MO2) and file date, used to judge whether a patch is outdated."""
            import datetime
            try:
                f = Path(index[n.lower()])
                d = datetime.date.fromtimestamp(f.stat().st_mtime)
                ver = read_meta(Path(mods_dir) / f.relative_to(mods_dir).parts[0]).get("version") if mods_dir else None
                return {"date": d, "version": ver}
            except (OSError, ValueError, KeyError):
                return None

        def mod_of(n):
            try:
                return Path(index[n.lower()]).relative_to(mods_dir).parts[0] if mods_dir else None
            except (ValueError, KeyError):
                return None
        overlaps, n_scanned, hubs, big, ignored, kinds = analyse_overlaps(
            plugins, active, index, args.min_masters,
            progress=lambda m: print(m, file=sys.stderr, end="\r"), mod_of=mod_of, loose=args.loose,
            info=plugin_info)
        print(" " * 50, file=sys.stderr, end="\r")
        ov_stats = (n_scanned, hubs, big, ignored, kinds)
    findings = analyse(plugins, active, inactive, args.min_masters, frozenset(index), cc_check)
    if ov_stats:   # name-based "same purpose" matches only make sense between two PATCHES, never a mod and its fix
        findings = [f for f in findings if f["kind"] != "duplicate_patch"
                    or all(ov_stats[4].get(x) == "patch" for x in f["plugins"])]
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
        elif t.startswith(("[CONFLICT", "[LIKELY", "== RED")):
            line = C["bold"] + C["red"] + line + C["reset"]
        elif t.startswith(("[SAME NAME", "[!]", "== YELLOW", "[POSSIBLY", "[A PATCH", "[TWO MODS", "[ONE IS", "[PATCHES", "[SAME DOWN")):
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
    ovs = overlaps or []
    red = [o for o in ovs if o["verdict"] == "CONFLICT"]
    yellow = [o for o in ovs if o["verdict"] == "OVERLAP"]
    green = [o for o in ovs if o["verdict"] in ("HARMLESS", "UNVERIFIED")]
    covered = [frozenset((o["first"], o["second"])) for o in red + yellow]
    dups = [f for f in dups if not any(set(f["plugins"]) <= set(pr) for pr in covered)]

    def plug_block(name, kind=None, role=""):
        det = "".join("<div>%s</div>" % (
            ('<a href="%s" target="_blank">%s</a>' % (e(l.split(": ", 1)[1].strip()), e(l)))
            if l.startswith("Nexus page") else e(l)) for l in describe_plugin(name, plugins, index, mods_dir))
        k = ('<span class="tag %s">%s</span>' % (kind, "PATCH" if kind == "patch" else "MOD")) if kind else ""
        return '<div class="pl"><b>%s</b> %s%s<div class="det">%s</div></div>' % (e(name), k, role, det)

    def rec_li(c, verb):
        extra = ('<div class="muted">%s</div>' % e(pos_text(c["pos"]))) if c.get("pos") else ""
        return '<li><b>%s</b> %s <span class="muted">(from %s)</span> %s: <b>%s</b>%s</li>' % (
            e(c["record"]), e(c["formid"]), e(c["master"]), verb, e(flist(c["lost"])), extra)

    def ov_card(o, cls):
        items = "".join(rec_li(c, "both change") for c in o["conflicts"][:6])
        items += "".join(rec_li(c, "later one undoes the earlier one's change to") for c in o["reverts"][:6])
        n = o["n_conflicts"] + o["n_reverts"]
        more = ("<li>... and %d more records</li>" % (n - 12)) if n > 12 else ""
        if cls == "strong":
            badge = '<span class="badge strong">RIVAL PATCHES</span>'
        elif o["obsolete"]:
            badge = '<span class="badge partial">POSSIBLY OBSOLETE PATCH</span>'
        else:
            badge = '<span class="badge partial">%s</span>' % e(o["relation"].upper())
        hints = "".join("<li>%s</li>" % e(h) for h in o["hints"])
        return ('<div class="card %s"><div class="hd">%s %s</div>%s%s<ul class="lost">%s%s</ul>'
                '<ul class="hint">%s</ul></div>') % (
            cls, badge, ("%d record(s)" % n) if n else "",
            plug_block(o["first"], o["kinds"][0], ' <span class="tag lose">loads first</span>'),
            plug_block(o["second"], o["kinds"][1], ' <span class="tag win">loads later &middot; wins</span>'),
            items, more, hints)

    CAP = 150
    red_cards = [ov_card(o, "strong") for o in red[:CAP]]
    for f in dups:
        lab = {"HIGH": "LIKELY DUPLICATES", "NAME": "SAME NAME, DIFFERENT REQUIREMENTS"}[f["confidence"]]
        nw = newest_hint(f["plugins"], index)
        red_cards.append('<div class="card %s"><div class="hd"><span class="badge %s">%s</span></div>%s'
                         '<ul class="hint"><li>Open each mod\'s Nexus page, read the description and changelog, disable ONE and test.%s</li></ul></div>' % (
                             "strong" if f["confidence"] == "HIGH" else "partial",
                             "strong" if f["confidence"] == "HIGH" else "partial", lab,
                             "".join(plug_block(x) for x in f["plugins"]),
                             (" Hint only: the most recently dated file is <b>%s</b> (newer is not always better)." % e(nw)) if nw else ""))
    yellow_cards = [ov_card(o, "partial") for o in yellow[:CAP]]
    if len(yellow) > CAP:
        yellow_cards.append('<p class="muted">Showing the %d most relevant of %d. Use the filter box to search.</p>' % (CAP, len(yellow)))
    qrows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        e(o["first"]), e(o["second"]),
        e("; ".join(sorted(o["harmless"])) if o["harmless"] else "original record not found, could not check"))
        for o in green)
    mrows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (e(f["plugins"][0]), e(f["master"]), e(f["reason"]))
                    for f in miss)
    nscan = ov_stats[0] if ov_stats else 0
    n_red = len(red) + len(dups)
    miss_note = ('<a class="note2 warn" href="#missing">%d missing master(s), click to see</a>' % len(miss)) if miss \
        else '<span class="note2 ok">No missing masters</span>'
    return """<!doctype html><html><head><meta charset="utf-8"><title>Redundant Patch Finder</title><style>
html{scroll-behavior:smooth}body{background:#14161a;color:#e6e6e6;font:15px/1.5 Segoe UI,system-ui,sans-serif;padding:24px;max-width:1000px;margin:auto}
h1{margin:0 0 4px}h2{margin-top:34px;border-bottom:1px solid #333;padding-bottom:6px;scroll-margin-top:12px}.muted{color:#8a8f98;font-size:13px}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0 8px}a.tile{flex:1;min-width:170px;background:#1e2127;border-radius:10px;padding:14px;border-left:6px solid #4caf50;color:#e6e6e6;text-decoration:none;cursor:pointer}
a.tile:hover{background:#252932}a.tile b{font-size:30px;display:block}a.tile.red{border-color:#ef5350}a.tile.yellow{border-color:#ffb300}a.tile span{font-size:13px;color:#9aa0a8}
.note2{font-size:14px}.note2.ok{color:#81c784}.note2.warn{color:#ffb300}
.card{background:#1e2127;border-radius:10px;padding:14px 16px;margin:12px 0;border-left:5px solid #666}
.card.strong{border-color:#ef5350}.card.partial{border-color:#ffb300}
.badge{font-weight:700;font-size:12px;padding:3px 8px;border-radius:5px;color:#111;margin-right:6px}
.badge.strong{background:#ef5350}.badge.partial{background:#ffb300}
.pl{margin:8px 0 0 6px;padding-left:10px;border-left:2px solid #333}.det{font-size:13px;color:#9aa0a8}
.tag{font-size:11px;padding:1px 7px;border-radius:9px;margin-left:6px;font-weight:600}.tag.lose{background:#4a2a2a;color:#ff8a80}.tag.win{background:#23402a;color:#a5d6a7}
.tag.mod{background:#1f3a5f;color:#90caf9}.tag.patch{background:#4a3b12;color:#ffd54f}
ul.lost{margin:10px 0 0 4px;font-size:13px;color:#c9ccd1}ul.hint{margin:10px 0 0 4px;font-size:14px;color:#b0b5bd}details{margin-top:8px}summary{cursor:pointer;color:#64b5f6}
a{color:#64b5f6}table{width:100%%;border-collapse:collapse}td{padding:6px 8px;border-bottom:1px solid #2a2d33;font-size:14px}
.ok{color:#81c784}input{width:100%%;padding:10px;border-radius:8px;border:1px solid #333;background:#1e2127;color:#eee;font-size:15px;box-sizing:border-box}
</style></head><body>
<h1>Redundant Patch Finder <span class="muted">v%s</span></h1>
<div class="muted">Scanned %d active plugins, compared the contents of %d plugins. A MOD is a base (like Lux Via). A PATCH fixes how mods work together. This tool only suggests candidates and never changes your files.</div>
<div class="tiles"><a class="tile red" href="#red"><b>%d</b>Red: rival patches that clash<br><span>Two patches fixing the same thing. Keep one.</span></a>
<a class="tile yellow" href="#yellow"><b>%d</b>Yellow: patches editing the same thing<br><span>Patches editing the same records differently, or a patch the mod made useless.</span></a>
<a class="tile" href="#green"><b>%d</b>Green: no problem<br><span>They overlap but do not clash.</span></a></div>%s
<input id="q" placeholder="Filter by plugin name..." oninput="f()" style="margin-top:14px">
<h2 id="red">Red: rival patches that clash</h2><div class="cards">%s</div>
<h2 id="yellow">Yellow: patches editing the same thing</h2><div class="cards">%s</div>
<h2 id="green">Green: compared, no problem (%d)</h2><details><summary>Show the list</summary><table>%s</table></details>
<h2 id="missing">Missing masters</h2><table>%s</table>
<script>function f(){var q=document.getElementById('q').value.toLowerCase();document.querySelectorAll('.cards .card').forEach(function(c){c.style.display=c.textContent.toLowerCase().indexOf(q)<0?'none':''})}</script>
</body></html>""" % (VERSION, total, nscan, n_red, len(yellow), len(green), miss_note,
        "".join(red_cards) or '<p class="ok">No rival patches clash.</p>',
        "".join(yellow_cards) or '<p class="ok">Nothing to review.</p>',
        len(green), qrows or '<tr><td class="ok">None</td></tr>',
        mrows or '<tr><td colspan="3" class="ok">None found</td></tr>')


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
