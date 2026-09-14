# NMS Mod Check

Tells you which of your No Man's Sky mods need attention after a game update: which ones broke, which are only partly outdated, and which have a newer version on Nexus Mods.

It reads your mod folders and the game's own files, asks Nexus Mods about each mod, and writes an HTML report. It never changes anything in your game folder.

## Requirements

- **Windows**, with No Man's Sky installed through **Steam**. GOG and Xbox / Game Pass installs are not supported: the checker uses Steam's build number to tell which game version you have.
- **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/). Tick "Add python.exe to PATH" during setup.
- Internet access. On the first run the checker downloads what it needs by itself: `hgpaktool` and `zstandard` (to read the game's `.pak` archives) and the matching [MBINCompiler](https://github.com/monkeyman192/MBINCompiler) release. No Nexus account or API key is needed.

## Use it

1. Download this repository (**Code > Download ZIP**) and unzip it anywhere.
2. Double-click **Check NMS mods.bat**.

The first run after a game update takes a few minutes while it caches the game files it needs. Later runs take about ten seconds. `report.html` then opens in your browser. Run it again after every game update.

Mods in `GAMEDATA\MODS` get one of these:

| Status | Meaning |
|---|---|
| Get the update | Broken or partly outdated, and the author already uploaded a newer version. Download it. |
| Disable for now | Broken by this game version and no fix posted yet. Move it to `GAMEDATA\MODS_disabled`. |
| Remove | Can never work in current NMS (for example an old `.pak` mod, or an empty folder). |
| Remove duplicate | Two copies of the same mod; the older copy is flagged. |
| Partly outdated | Mostly works, but some settings no longer exist, or it carries an old copy of a table the update extended. |
| Optional update | Still compatible, but Nexus has a newer version than your copy. |
| OK | Compatible. Nothing to do. |

Create a `GAMEDATA\MODS_disabled` folder to park broken mods. The game never looks there, but the checker keeps checking them:

| Status | Meaning |
|---|---|
| Fix is out (disabled mod) | The author posted a version after this game update. Install it, then delete the disabled copy. |
| Works again (disabled mod) | Compatible again. Move it back into `MODS`. |
| Still broken (disabled) | Nothing new yet. Leave it. |
| Safe to delete (disabled) | You have a newer copy in `MODS`, or it can never work. |

Open "details" in any row to see the evidence. The Nexus column links straight to the mod's Files tab.

Under each mod's name the report also says where it came from:

- **Vortex**: installed by [Vortex](https://www.nexusmods.com/about/vortex/). Update, disable and remove it in Vortex, not by moving its folder.
- **Web**: a folder you unzipped into `MODS` yourself.

## Updating a mod

- **Vortex mod:** click **Mod Manager Download** on the mod's Nexus Files tab and pick **Replace** when Vortex asks what to do with the old version.
- **Web mod:** delete the old folder (or move it to `MODS_disabled`) **before** installing the new version, by hand or with Vortex. New versions often use the same folder name, and files the new version no longer has would stay behind and keep loading.

## Linking mods to Nexus

The checker needs each mod folder's Nexus mod ID. It finds it by itself when the archive you installed from is still in a download folder, because Nexus file names contain the mod ID: `Lumen-1600-6-42-1780016618.zip` is mod 1600. It looks in your `Downloads` folder and in Vortex's default No Man's Sky download folder.

If your archives are somewhere else, copy `config.example.json` to `config.json` and list those folders under `download_dirs`. Leave out `game_dir` unless the checker can't find your game.

Folders it can't link show up as "Not linked", with likely Nexus matches. Add them by hand to `mods.json` (created on the first run), using the number from the mod's Nexus page address:

```json
"mods": {
  "My Mod Folder": {"nexus_id": 1234}
}
```

If a mod passes the checks but breaks your game when you play, flag it so the checker keeps reporting it as broken until you install a newer copy:

```json
"My Mod Folder": {"nexus_id": 1234, "known_bad": {"found": "2026-09-10", "note": "froze the game after loading a save."}}
```

## How it decides

- **Nexus:** the newest files of each mod through Nexus's public API, compared with the moment the installed Steam build went live and with when your copy was installed.
- **Binary files (.MBIN):** the template GUID in the header is compared with the game's own file for the same path, read straight from the game's `.pak` archives. Hello Games changes it whenever a data layout changes, so a mismatch means the file was built for an older game version.
- **Text files (.EXML / .MXML):** every field a mod sets is looked up in the game's current data (MBINCompiler decompiles of the game's own files). Fields that no longer exist do nothing.
- **Full table copies:** if a mod carries most of a game table and the game has since added entries to it, those entries are listed.
- **Vortex:** a mod counts as a Vortex mod when it is listed in `MODS\vortex.deployment.json`.

## Turning a mod off without moving it

Open `Binaries\SETTINGS\GCMODSETTINGS.MXML`, find the mod's `Name` and set `Enabled` to `false`. If two mods replace the same file, the one with the lower `ModPriority` number wins. Deleting that file makes the game rebuild it with every mod enabled.

## If the game crashes with mods

Move half of your mods out of `MODS` and launch the game. If it runs, the bad mod is in the other half; if it still crashes, it is in this half. Keep halving until one mod is left. After a crash the game may turn all mods off for the next launch, so check that mods are on again before each test.

## Files

- `nms_mod_check.py`: the checker. `--no-open` skips opening the browser, `--offline` skips Nexus and GitHub.
- `Check NMS mods.bat`: double-click launcher.
- `config.example.json`: copy to `config.json` for custom download folders or game location.
- `mods.json`: your mod folder to Nexus ID list, created and updated on each run.
- `report.html`: the latest report.
- `%LOCALAPPDATA%\nms-mod-checker`: cached game files per Steam build, MBINCompiler, hgpaktool and `last_check.json`. Safe to delete; it is rebuilt on the next run.

## Limits

- A mod can be technically valid and still behave badly with new content, or freeze the game. The check finds format and field problems, not design clashes. Always test after an update.
- Custom files with no game counterpart (new models and so on) can only be checked by file type.
- The text-file check needs an MBINCompiler release for the current game build. Right after a patch that can take a day or two; the binary and Nexus checks work regardless.

## License

MIT, see [LICENSE](LICENSE). Not affiliated with Hello Games or Nexus Mods.
