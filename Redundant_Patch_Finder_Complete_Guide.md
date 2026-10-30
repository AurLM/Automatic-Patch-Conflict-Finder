# Redundant Patch Finder - Complete Guide

Parts: (1) updated "What it does" text for the Nexus page, (2) exactly how the tool works, (3) how to continue the project, (4) how to publish it on Nexus and how MO2 users install and run it.

Honest status: the logic and speed are tested on synthetic plugin files I built. It has been run on your real list (1,045 plugins), which exposed and fixed two real bugs (originals not found, too much yellow). It has never been run on a real list with a true rival pair (red), and the .exe has not been built or tested yet.

---

## PART 1 - Text to ADD to the Nexus description (BBCode)

Paste this near the top of the description, right after the intro paragraph.

```
[size=4][b]What it actually does[/b][/size]
Most conflict tools only tell you that two plugins "touch the same record". On a big list that is thousands of lines and almost all of it is harmless. Redundant Patch Finder goes further:

[list]
[*][b]It opens the plugins.[/b] It reads the binary content of every active .esp/.esm/.esl, finds every record each plugin changes, and compares what each plugin changes, field by field.
[*][b]It finds clashes nobody has documented.[/b] It does not rely on a hand-written database of known incompatibilities. If two obscure patches clash and no one has ever written it down, it can still find it.
[*][b]It treats patches and base mods differently.[/b] A base mod (Lux Via, Lux Orbis...) is the foundation. A patch fixes how mods work together. Only clashes BETWEEN PATCHES are raised as problems, because a patch editing a mod's records is exactly what a patch is for.
[*][b]It checks against the original.[/b] Two plugins changing the same record is not a problem by itself. The tool looks up the original record in the master and only flags a pair when both change the same field to DIFFERENT values.
[*][b]It understands layering.[/b] Patches built on other patches, patches from the same download and "bridge" patches that tie several mods together are recognised and not flagged as conflicts.
[*][b]It can flag a patch an updated mod has made obsolete.[/b] If a mod already contains everything a patch does (same values), or the mod is clearly newer than the patch, the tool tells you the patch may be unnecessary, and why.
[/list]
```

Also add to the FAQ:

```
[b]How is this different from LOOT or xEdit?[/b]
LOOT sorts your load order and shows notes that people have written for known plugins. xEdit shows every record conflict, one by one, with no idea which plugins are patches. This tool compares the actual contents of your plugins, knows the difference between a patch and a base mod, and boils a 1,000-plugin list down to a short list of pairs worth reading.
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

### 2.8 Output
- Console with colours, `patch_report.txt`, and `patch_report.html` (three clickable tiles, a filter box, cards with details, Nexus links, positions, dates and hints). A `NOTE:` at the top says if some originals could not be checked, and why.
- `--json` for other tools.
- Double-clicking the program (no arguments) writes both reports next to it and opens the HTML in the browser.

### 2.9 Options (command line)
`--instance` (MO2 folder), `--profile`, `--game-data`, `--data`, `--plugins-txt`, `--min-masters`, `--aggressive` (show weak name-based matches), `--loose` (show patches for different mods), `--no-scan`, `--html`, `--no-browser`, `--no-color`, `--json`, `--out`.

### 2.10 Limits (be honest on the page)
- Only compares plugin records. It cannot see scripts, meshes, textures, SKSE, or INI conflicts.
- Mod-vs-patch classification is by name. It can be wrong.
- The 2-unit, 60 KB and 30-day thresholds are my guesses.
- Cannot know if a patch is "correct" for your setup. It reports overlaps and clashes only.
- Not tested yet on a real rival pair.

---

## PART 3 - How to continue the project

Priority order (best value for downloads and effort):

1. **Get real red results.** Install a known rival pair (for example the two bannered mare patches you mentioned) and confirm red. Screenshot it for the Nexus page. Optional: I can build fake rival test plugins.
2. **Headline feature: "Patches you can remove."** Add a dedicated section listing obsolete patches with a clear reason, and a "total plugins you could free" count. Easy to explain, easy to screenshot.
3. **Fix remaining "original not found."** Use the NOTE lines in the report (Nature of the Wild Lands, Riften Expansion) to find what is unusual about those masters.
4. **Extra evidence layers.**
   - LOOT masterlist incompatibilities (no key needed).
   - Optional Nexus API key to read a patch's description and quote lines like "do not use with X". Only look up plugins in red/yellow pairs, cache results, and show a quote, never a verdict.
5. **Missing-patch finder.** Find mod pairs that clash with no patch tying them together. This is your earlier "patches that don't exist yet" idea. Noisy, so apply limits (two real mods, many shared records, no bridge).
6. **Usability.** A folder picker window for non-MO2 users, Vortex Nexus IDs, a "what changed since last run" comparison, and a "copy disable list" button.
7. **Quality.** Add automated tests (the synthetic generators already built can become the test suite), version numbers in the report, and a "report a problem" button that copies the NOTE text.

How I recommend working together:
- You run it and send `patch_report.txt` plus screenshots.
- I adjust thresholds and rules from the real output.
- After each change, I rerun my 100/1,000/3,000-plugin synthetic tests so nothing regresses.

---

## PART 4 - Publishing on Nexus, and using it with MO2

### 4A. Build the .exe (GitHub Actions)
1. In your repository keep `redundant_patch_finder.py` and `.github/workflows/build-windows-exe.yml`.
2. Push to `main` with GitHub Desktop. The "Build Windows exe" workflow runs on every push (or use Actions -> Build Windows exe -> Run workflow).
3. Wait for the green check. Open the run, scroll to **Artifacts**, download **Redundant-Patch-Finder-windows**. Unzip it. The file is `Redundant Patch Finder.exe`.
4. Test it BEFORE uploading, on your own PC and ideally another one: double-click it from your MO2 folder, check the report opens, and run it with `--help` in a terminal.
5. Windows SmartScreen will likely warn because the file is not signed: More info -> Run anyway. Some antivirus tools false-flag PyInstaller files. Scan it yourself on virustotal.com and be ready to explain.

### 4B. Prepare the Nexus upload package
Make a ZIP (name it `Redundant Patch Finder 0.3.0.zip`) containing:
- `Redundant Patch Finder.exe`
- `README.txt` (short usage, the SmartScreen note, how to send a bug report)
- optionally `Run Patch Finder.bat` (for people who prefer the Python version) and the `.py` source, if you choose to publish it.
Do NOT put a `Data` folder or an .esp in it. It is a tool, not a mod.

### 4C. Upload on Nexus Mods
1. Log in on nexusmods.com, open the Skyrim Special Edition site, click your profile -> **Add a mod** (or the Upload button on the Skyrim SE page).
2. Fill in: name, category (Modders Resources and Tutorials or Utilities), version, short description (tagline), the full description from the Nexus text file, tags.
3. Upload images (screenshots of the red / yellow / green tiles and a card). Mark the first one as the main image. Remove personal info from screenshots.
4. Upload the ZIP as the **main file**, type **Main files**, set a version, and write a short file description.
5. Set permissions (your choice) and add credits.
6. Set the mod to hidden while you check it, then publish. Check the page with the "Preview" before making it public. Nexus runs an automatic virus scan; a new unsigned .exe can take longer to approve.
7. Nexus may ask you to confirm it is not a re-upload and that you own the work.

### 4D. How MO2 users install and run it
Two simple ways. Tell users the first one on the page.

**Way 1 - Simplest (recommended): run it from the MO2 folder**
1. Download the ZIP manually (not the Mod Manager button).
2. Extract `Redundant Patch Finder.exe` into the MO2 folder, the one that contains `ModOrganizer.ini` and the `mods` and `profiles` folders.
3. Double-click it. It reads the selected profile and your active plugins, writes `patch_report.html` / `.txt` next to it, and opens the report in the browser.
Why this works: the tool reads MO2's real folders on disk, so it does not need MO2 running.

**Way 2 - Run it inside MO2 (shows up in the executables drop-down)**
1. In MO2, click the puzzle-piece icon (Install a new mod from an archive) and pick the ZIP, or use "Mod Manager Download" on Nexus. MO2 creates a mod folder with the .exe inside. Leave it enabled or disabled; it contains no plugin.
2. Click the gear icon (**Edit Executables**) -> **+** -> **Add from file...**, and browse to `mods\Redundant Patch Finder\Redundant Patch Finder.exe`.
3. Give it a title, then OK. It now appears in the executables drop-down next to Run.
4. Select it and click **Run**.
Important caveats (I have not tested these in MO2):
- When a program is launched through MO2, files it writes go into MO2's `overwrite` folder, so find `patch_report.html` there.
- The tool finds the MO2 instance by looking for `ModOrganizer.ini` in or above where it runs. If it cannot, run it with `--instance "C:\Path\To\MO2"`. In MO2's executable settings, put that in the Arguments box.
- Running inside MO2's virtual file system is not required, and Way 1 is safer.

### 4E. After publishing
- Pin a comment on the page saying: "Found a problem? Paste the first lines of patch_report.txt."
- Add a Sticky post explaining that results are suggestions, not verdicts.
- Update the file version and changelog each time you change the code.
- Reply to bug reports with the NOTE text.

### 4F. Checklist before you click Publish
- [ ] .exe built from the LATEST `.py`, tested on a clean PC
- [ ] Real red result screenshot (or clearly say "not yet seen on a real pair")
- [ ] Description includes the new "What it actually does" text
- [ ] SmartScreen / antivirus note on the page
- [ ] Screenshots free of personal paths
- [ ] Permissions and credits set
