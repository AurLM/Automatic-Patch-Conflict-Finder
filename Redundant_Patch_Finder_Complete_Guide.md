# Redundant Patch Finder - Complete Guide

---

## PART 1 - Description

```
What it actually does

Most conflict tools only tell you that two plugins "touch the same record". On a big list that is thousands of lines and almost all of it is harmless. Redundant Patch Finder goes further:

It opens the plugins. It reads the binary content of every active .esp/.esm/.esl, finds every record each plugin changes, and compares what each plugin changes, field by field.
It finds clashes nobody has documented. It does not rely on a hand-written database of known incompatibilities. If two obscure patches clash and no one has ever written it down, it can still find it.
It treats patches and base mods differently.A base mod (Lux Via, Lux Orbis...) is the foundation. A patch fixes how mods work together. Only clashes BETWEEN PATCHES are raised as problems, because a patch editing a mod's records is exactly what a patch is for.
It checks against the original. Two plugins changing the same record is not a problem by itself. The tool looks up the original record in the master and only flags a pair when both change the same field to DIFFERENT values.
It understands layering. Patches built on other patches, patches from the same download and "bridge" patches that tie several mods together are recognised and not flagged as conflicts.
It can flag a patch an updated mod has made obsolete. If a mod already contains everything a patch does (same values), or the mod is clearly newer than the patch, the tool tells you the patch may be unnecessary, and why.

```

FAQ:

```
How is this different from LOOT or xEdit?
LOOT sorts your load order and shows notes that people have written for known plugins. xEdit shows every record conflict, one by one, with no idea which plugins are patches. This tool compares the actual contents of your plugins, knows the difference between a patch and a base mod, and boils a 1,000-plugin list down to a short list of pairs worth reading.

Does it change my load order or delete anything?
No. It is read-only.

It says two patches clash. Do I have to remove one?
Not automatically. The tool suggests candidates. Always read the patches' Nexus pages: some authors say "use this instead of that one", others say "load both".

A pair is listed as "original record not found".
The tool could not find the vanilla/base record to compare against, so it cannot say who is right. These pairs are kept out of the way unless they look like rival patches. If you see many, please tell me (see Bug reports).

Does it handle ESL / light plugins and Creation Club content?
Yes. Creation Club files listed in Skyrim.ccc are recognised and named.

Will it find every incompatibility?
No. It finds plugins that change the same records in different ways. It cannot detect script, mesh or texture conflicts, and it cannot tell you what is "correct" in your game, only what overlaps. Treat it as a guide, not a verdict.

Is it fast?
Yes. In my tests, 1,000 plugins take about a quarter of a second to compare on test data, and the 3,000-plugin test was around half a second. Real lists with large mods take a few seconds.
```
---

## PART 2 - How it works, step by step

### 2.1 Finding your plugins
- **MO2:** it reads `ModOrganizer.ini` (selected profile and game path), then `profiles/<name>/plugins.txt` (a `*` marks an active plugin) and `modlist.txt`. Each plugin is located inside its mod folder under `mods/`, with the folder name used as the "MO2 mod" name. It reads each mod's `meta.ini` for the Nexus ID and version, so cards can show the Nexus link.
- **Vortex / Wrye Bash / manual:** it reads the Skyrim `Data` folder and `plugins.txt` (in `%LOCALAPPDATA%\Skyrim Special Edition...`). It finds the game folder from the registry, Steam or GOG. It understands Wrye Bash `.ghost` files and Creation Club content (`Skyrim.ccc`), and gives CC plugins friendly names.
- Result: the list of active plugins, in load order, with the file for each.

### 2.2 Reading a plugin (the binary format)
A Skyrim plugin is a header (`TES4`) with the list of masters, followed by records. Each record has a 24-byte header (type, size, flags, FormID) and a set of subrecords (the fields: position, lighting, water, grid...). Some records are compressed (zlib). The tool reads all of this itself, with no external library.
- The FormID's first byte says which master the record belongs to. If it points to a master, the plugin is **changing** an existing record (an "override"). If it points to the plugin itself, the record is **new**.
- Only overrides matter for conflicts. New records are only counted (they help tell a mod from a patch).

### 2.3 Mod or patch?
A plugin is classified as a **patch** if:
1. its name or its MO2 folder contains patch / fix / compat / integration / resolution (also inside CamelCase words, and not inside another word), or
2. it is tiny (under about 60 KB), creates no new records, and needs at least two non-vanilla masters.
Everything else is a **mod**. This is a heuristic. It can be wrong, for example a mod with "fix" in its name.

### 2.4 Which pairs to compare
For every record, the tool lists which plugins change it. Two plugins that change the same record form a "pair". A record changed by more than 150 plugins is ignored as noise (it is reported as ignored).

### 2.5 The three-way comparison (the key idea)
For each shared record the tool compares three versions:
- the ORIGINAL record (found in the master that created it, following "override of an override" chains down to the first definition),
- the EARLIER plugin's version,
- the LATER plugin's version.

For each field:
- **Clash:** both changed it, compared to the original, to different values.
- **Revert:** the later plugin put the field back to the original value, undoing the earlier one.
- **Harmless:** identical, or one of them did not change that field.
- Positions (placed objects) within 2 game units and almost the same rotation are treated as "the same in practice". Master-index renumbering between plugins is handled so the same reference is not mistaken for a difference.
- If the original cannot be found, the pair is marked "could not check", and is only shown (as yellow) for rival patches.

### 2.6 How pairs are sorted into red / yellow / green
Relation between the two plugins:
- one lists the other as a master: "built on the other" (never red)
- same MO2 mod folder: "same download" (never red)
- mod + patch: "a patch applied on top of a mod"
- two mods: "two mods editing the same thing"
- two patches with identical non-vanilla master lists: "rival patches for the same mods"
- two patches where one covers the other's mods plus more: "one patch covers more, it may replace the smaller one"
- other patches: "patches for different mods"

Verdicts:
- **Red:** rival patches (same masters) that clash, AND no third patch that loads after both and edits the same records. A later patch editing the same records is treated as a bridge that ties them together.
- **Yellow:** rival patches that differ but cannot be checked; patches where one covers more; a patch the mod has made obsolete. With `--loose`, also patches for different mods.
- **Green:** everything else, including mod vs mod, patch on a mod, and patches for different mods that merely touch the same cells.

### 2.7 "Obsolete patch" and "mod is newer" hints
For a patch loaded after the mod it patches:
- if every record the patch edits is also edited by the mod, with identical or near-identical values, the patch is flagged **possibly obsolete**;
- otherwise, if the mod's file or MO2 version date is 30+ days newer than the patch, the card says the mod may already include the fix;
- if the patch loads BEFORE the mod, the card says the mod overrides the patch.
These are hints. They cannot see inside a mod's changelog.

### 2.10 Limits 
- Only compares plugin records. It cannot see scripts, meshes, textures, SKSE, or INI conflicts.
- Mod-vs-patch classification is by name. It can be wrong.
- The 2-unit, 60 KB and 30-day thresholds are my guesses.
- Cannot know if a patch is "correct" for your setup. It reports overlaps and clashes only.


KEEP IN MIND THIS IS AN ALPHA VERSION OF WHAT IS TO COME !!!
