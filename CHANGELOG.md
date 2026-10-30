# Conflicting Patch Finder - Changelog

## 0.5.0-alpha  (renamed from "Redundant Patch Finder")
**New: "two plugins decorate the same room" check (yellow)**
- Reads which interior cell each NEW object (REFR / ACHR / ACRE) is added to.
- Two plugins that each add 5+ new objects to the same interior room are flagged.
- Not flagged: one built on the other (lists it as a master), the same download, or a third plugin made for both (a compatibility patch).
- Catches interior mods that never edit the same record but still stack furniture in one room (e.g. two Whiterun interior overhauls).

**Red parsing (how a pair becomes RED)** - all of these must be true:
1. Both plugins are PATCHES (name/MO2-folder keywords such as patch, fix, compat, integration, resolution; or tiny, no new records, 2+ mods required).
2. Same set of non-vanilla masters ("rival patches for the same mods").
3. Neither lists the other as a master, and they are not from the same MO2 mod folder.
4. They change the same field of the same record to different values, compared against the ORIGINAL record (three-way compare).
5. No third patch loading after both edits the same records (a "bridge" patch clears the clash).
- Same-name patch groups (e.g. two Ruined Tundra Farmhouse patches from two different packs) are also shown in red.
- All red cards now have a red edge (the same-name type used to be orange).

**Other changes**
- Yellow is "Worth a look": rival patches that differ but cannot be checked, a patch that covers the same mods plus more, a patch the mod has made obsolete, and rooms decorated twice. "Patches for different mods" go to green (use `--loose` to show them).
- Original records are now found by following "override of an override" chains down to the plugin that first defined them.
- The report now says why originals could not be checked (master missing on disk / record not found / unreadable).
- Renamed: file `conflicting_patch_finder.py`, program "Conflicting Patch Finder.exe".

## 0.3.x
- Red / yellow / green tiers, clickable tiles that jump to the section.
- Mods and patches are treated differently; only clashes between patches are red.
- Possibly-obsolete patch hints (mod already contains the patch's values, mod newer than patch, patch loads before the mod).
- Position details (how far each plugin moves an object).
- Faster scans (1,000 plugins in about a quarter of a second on test data).

## 0.2.x
- Three-way compare against the original record instead of "two plugins touch the same record".
- Competing-patch rule: only patches for the same mods that are not built on each other.
- HTML report, friendlier console colours, double-click mode, Creation Club names.

## 0.1.x
- First version: name-based duplicate patch groups and missing masters, MO2 and Data-folder support.

Known limits: only compares plugin records; patch/mod classification is by name; thresholds are estimates; not yet tested on a real rival-patch pair beyond the Ruined Tundra Farmhouse same-name case.
