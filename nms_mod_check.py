#!/usr/bin/env python3
"""NMS Mod Check: which of your No Man's Sky mods need attention after a game update.

For every folder in GAMEDATA/MODS (and in GAMEDATA/MODS_disabled, where broken
mods are parked) it combines two independent signals:

1. Nexus Mods status (public API, no account or key needed): the newest file,
   and whether the author uploaded anything after the current game build went live.
2. A compatibility test against the game's own files for the build you have installed:
   * .MBIN files: the template GUID in the header must match the game's current file.
     Hello Games changes it whenever a data layout changes, so a mismatch means the
     file was built for an older game version.
   * .EXML / .MXML files: every field the mod sets must still exist in the current
     game data (checked against MBINCompiler decompiles of the game's own files).
   * Full copies of big tables: entries the game added that the mod's copy lacks.

Usage:  python nms_mod_check.py            writes report.html next to this script and opens it
        python nms_mod_check.py --no-open  same, without opening the browser
        python nms_mod_check.py --offline  skip Nexus and GitHub lookups
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import html
import importlib
import json
import os
import re
import struct
import subprocess
import sys
import urllib.request
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LOCALAPPDATA') or HERE) / 'nms-mod-checker'
LIB = DATA / 'lib'
MAPPING_FILE = HERE / 'mods.json'
MAPPING_HELP = ('Links each folder in GAMEDATA/MODS to its Nexus page (nexusmods.com/nomanssky/mods/<nexus_id>). file_ts is '
                'the upload time from the Nexus archive name, used to spot newer files than your copy. New folders are linked '
                'automatically when their Nexus-named archive is in a download folder; otherwise add them here by hand. A mod '
                'that broke the game in testing gets "known_bad": {"found": "YYYY-MM-DD", "note": "..."}; the checker then '
                'keeps it marked broken until a copy installed after that date appears.')
CONFIG_FILE = HERE / 'config.json'
REPORT_FILE = HERE / 'report.html'
DISABLED_DIR = 'MODS_disabled'   # sibling of GAMEDATA/MODS; the game never looks in here

APP_ID = '275850'
NEXUS_GAME_ID = 1634
NEXUS_DOMAIN = 'nomanssky'
NEXUS_GQL = 'https://api.nexusmods.com/v2/graphql'
MBINC_RELEASES = 'https://api.github.com/repos/monkeyman192/MBINCompiler/releases?per_page=12'
UA = {'User-Agent': 'nms-mod-check/1.1'}

MBIN_MAGIC = (0xCCCCCCCCCCCCCCCC, 0xDDDDDDDDDDDDDDDD)
MBIN_VERSION = 3300
GAME_TOP_DIRS = {'metadata', 'models', 'textures', 'ui', 'fonts', 'audio', 'materials', 'scenes',
                 'shaders', 'pipelines', 'language', 'globals', 'custommodels', 'tpfsdict', 'music'}
# Runtime-only handle that MBINCompiler never writes out; old decompiles still contain it.
IGNORED_FIELDS = {'ResHandle'}
ACTIVE_FILE_CATEGORIES = {'MAIN', 'UPDATE', 'OPTIONAL', 'MISCELLANEOUS'}
TYPE_RE = re.compile(r'^c?((?:Gc|Tk)[A-Za-z0-9_]+)(?:\.xml)?$')
NEXUS_ARCHIVE_RE = re.compile(
    r'^(?P<name>.+?)-(?P<id>\d{1,6})-(?P<ver>.+?)-(?P<ts>\d{10})(?: \(\d+\))?\.(?:zip|7z|rar)$', re.I)


# ----------------------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    tmp.replace(path)


def http_json(url, payload=None, timeout=60):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = dict(UA)
    if body is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def http_download(url, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.part')
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, 'wb') as fh:
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
    tmp.replace(dest)


def fmt_date(ts):
    if not ts:
        return '?'
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime('%Y-%m-%d')


def parse_xml(path):
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None


def norm_type(value):
    m = TYPE_RE.match(value or '')
    return m.group(1) if m else None


def read_header(data):
    if len(data) < 0x20:
        return None
    magic, mbinver, _hdr, namehash, guid, _ts = struct.unpack('<QHHIQQ', data[:0x20])
    return {'magic': magic, 'mbinver': mbinver, 'namehash': namehash, 'guid': guid,
            'built_with': '.'.join(str(b) for b in data[0x18:0x1C])}


def multi_suffix(rel):
    """'.../BLACKHOLE_CORE.SCENE.MBIN' -> '.scene.mbin' (None for plain '.mbin')."""
    base = rel.rsplit('/', 1)[-1].lower()
    if base.count('.') < 2:
        return None
    return base[base.index('.'):]


# ----------------------------------------------------------------------------- game + steam

def find_game_dir(cfg):
    if cfg.get('game_dir'):
        return Path(cfg['game_dir'])
    libraries = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            libraries.append(Path(winreg.QueryValueEx(key, 'SteamPath')[0]))
    except OSError:
        pass
    for steam in list(libraries):
        vdf = steam / 'steamapps' / 'libraryfolders.vdf'
        if vdf.exists():
            text = vdf.read_text(encoding='utf-8', errors='replace')
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                libraries.append(Path(m.group(1).replace('\\\\', '\\')))
    for lib in libraries:
        acf = lib / 'steamapps' / f'appmanifest_{APP_ID}.acf'
        if acf.exists():
            m = re.search(r'"installdir"\s+"([^"]+)"', acf.read_text(encoding='utf-8', errors='replace'))
            return lib / 'steamapps' / 'common' / (m.group(1) if m else "No Man's Sky")
    raise SystemExit('Could not find No Man\'s Sky. Create config.json with {"game_dir": "X:/.../No Man\'s Sky"}')


def read_manifest(game_dir):
    acf = game_dir.parent.parent / f'appmanifest_{APP_ID}.acf'
    text = acf.read_text(encoding='utf-8', errors='replace') if acf.exists() else ''

    def get(key):
        m = re.search(rf'"{key}"\s+"([^"]*)"', text)
        return int(m.group(1)) if m and m.group(1).isdigit() else 0
    return {'buildid': get('buildid'), 'lastupdated': get('lastupdated')}


def steam_public_build():
    try:
        d = http_json(f'https://api.steamcmd.net/v1/info/{APP_ID}', timeout=30)
        b = d['data'][APP_ID]['depots']['branches']['public']
        return {'buildid': int(b['buildid']), 'timeupdated': int(b['timeupdated'])}
    except Exception:
        return None


def read_mod_settings(game_dir):
    """The game's own mod list (enabled flag + priority), keyed by upper-case folder name."""
    root = parse_xml(game_dir / 'Binaries' / 'SETTINGS' / 'GCMODSETTINGS.MXML')
    out, disable_all = {}, False
    if root is None:
        return out, disable_all
    for prop in root:
        if prop.get('name') == 'DisableAllMods':
            disable_all = prop.get('value') == 'true'
        elif prop.get('name') == 'Data':
            for item in prop:
                vals = {q.get('name'): q.get('value') for q in item}
                if vals.get('Name'):
                    out[vals['Name'].upper()] = {'enabled': vals.get('Enabled') == 'true',
                                                 'priority': int(vals.get('ModPriority') or 0)}
    return out, disable_all


# ----------------------------------------------------------------------------- tools

def ensure_hgpaktool():
    sys.path.insert(0, str(LIB))
    try:
        from hgpaktool.api import HGPAKFile  # noqa: F401
        import zstandard  # noqa: F401
    except ImportError:
        print(f'First run: installing hgpaktool + zstandard into {LIB} ...')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check',
                               '--target', str(LIB), 'hgpaktool', 'zstandard'])
        importlib.invalidate_caches()
    from hgpaktool.api import HGPAKFile
    return HGPAKFile


def get_mbincompiler(buildid, offline):
    """Pick the MBINCompiler release whose test build is closest to (not newer than) ours."""
    sel_file = DATA / 'tools' / 'selected.json'
    selected = load_json(sel_file, {})
    info = selected.get(str(buildid))
    if info and Path(info['exe']).exists():
        return info
    if offline:
        return next((v for v in selected.values() if Path(v['exe']).exists()), None)
    try:
        releases = sorted(http_json(MBINC_RELEASES), key=lambda r: r.get('published_at') or '', reverse=True)
    except Exception as ex:
        print(f'  ! could not reach GitHub for MBINCompiler ({ex})')
        return next((v for v in selected.values() if Path(v['exe']).exists()), None)
    candidates, chosen = [], None
    for rel in releases:
        assets = {a['name']: a['browser_download_url'] for a in rel.get('assets', [])}
        if 'MBINCompiler.exe' not in assets or 'libMBIN.dll' not in assets:
            continue
        tested = None
        if 'report.json' in assets:
            try:
                tested = int(http_json(assets['report.json'])['version'])
            except Exception:
                pass
        cand = {'tag': rel['tag_name'], 'tested_build': tested, 'assets': assets}
        candidates.append(cand)
        if tested and tested <= buildid:
            chosen = cand
            break
    exact = chosen is not None
    chosen = chosen or (candidates[0] if candidates else None)
    if not chosen:
        return None
    folder = DATA / 'tools' / chosen['tag']
    for name in ('MBINCompiler.exe', 'libMBIN.dll'):
        if not (folder / name).exists():
            print(f'  downloading {name} {chosen["tag"]} ...')
            http_download(chosen['assets'][name], folder / name)
    info = {'tag': chosen['tag'], 'tested_build': chosen['tested_build'], 'exe': str(folder / 'MBINCompiler.exe'),
            'exact': exact}
    selected[str(buildid)] = info
    save_json(sel_file, selected)
    return info


def find_7zip():
    candidates = [Path(r'C:\Program Files\7-Zip\7z.exe'), Path(r'C:\Program Files (x86)\7-Zip\7z.exe')]
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
                          (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')):
            with winreg.OpenKey(hive, sub) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as app:
                            if '7-Zip' in str(winreg.QueryValueEx(app, 'DisplayName')[0]):
                                candidates.insert(0, Path(winreg.QueryValueEx(app, 'InstallLocation')[0]) / '7z.exe')
                    except OSError:
                        continue
    except OSError:
        pass
    return next((c for c in candidates if c.exists()), None)


# ----------------------------------------------------------------------------- mods on disk

def scan_mods(mods_dir, disabled=False):
    """Mod folders keyed by a unique id ('Lumen' or 'MODS_disabled/Lumen')."""
    mods = {}
    if not mods_dir.is_dir():
        return mods
    for entry in sorted(mods_dir.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        files = []
        for root, _dirs, names in os.walk(entry):
            for name in names:
                if name == '__folder_managed_by_vortex':
                    continue
                path = Path(root) / name
                rel = path.relative_to(entry).as_posix()
                files.append(describe_file(path, rel))
        st = entry.stat()
        key = f'{DISABLED_DIR}/{entry.name}' if disabled else entry.name
        # When this copy landed on disk: a fresh extract or Vortex deploy creates the folder anew.
        mods[key] = {'name': entry.name, 'path': entry, 'files': files, 'disabled': disabled,
                     'mtime': st.st_mtime, 'installed_at': max(st.st_mtime, st.st_ctime)}
    return mods


def vortex_folders(mods_dir):
    """{lowercased MODS folder: upload time of the Nexus file it came from (0 if unknown)} for what Vortex has
    deployed right now. Its __folder_managed_by_vortex marker files are no proof: they linger in hand-installed
    folders after an old Vortex deploy or purge."""
    try:
        doc = json.loads((mods_dir / 'vortex.deployment.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    out = {}
    for f in doc.get('files') or []:
        folder = f.get('relPath', '').replace('\\', '/').split('/')[0].lower()
        source = f.get('source') or ''
        parsed = parse_archive_name(source) or parse_archive_name(source + '.zip')
        out[folder] = max(out.get(folder, 0), parsed[1] if parsed else 0)
    return out


def describe_file(path, rel):
    up = rel.upper()
    entry = {'rel': rel, 'key': re.sub(r'\.(exml|mxml)$', '.mbin', rel.lower()), 'path': path}
    if '/' not in rel:
        entry['kind'] = 'ROOT'   # readme, lua, LocTable.MXML ... not a game path
    elif up.endswith('.MBIN') or up.endswith('.MBIN.PC'):
        entry['kind'] = 'MBIN'
        with open(path, 'rb') as fh:
            entry['hdr'] = read_header(fh.read(0x20))
    elif up.endswith('.EXML') or up.endswith('.MXML'):
        entry['kind'] = 'XML'
    elif up.endswith('.DDS'):
        entry['kind'] = 'DDS'
    elif up.endswith('.PAK'):
        entry['kind'] = 'PAK'
    else:
        entry['kind'] = 'OTHER'
    if up.endswith('.PAK'):
        entry['kind'] = 'PAK'
    if up.endswith('.LUA'):
        entry['lua'] = True
    return entry


def layout_problem(mod):
    game_files = [f for f in mod['files'] if f['kind'] in ('MBIN', 'XML', 'DDS')]
    if any(f['kind'] == 'PAK' for f in mod['files']) and not game_files:
        return 'Old .pak mod. NMS stopped loading .pak mods in 2022 (v4.0), so this does nothing.'
    tops = {f['rel'].split('/')[0].lower() for f in mod['files'] if '/' in f['rel']}
    if game_files and not (tops & GAME_TOP_DIRS):
        seconds = {f['rel'].split('/')[1].lower() for f in mod['files'] if f['rel'].count('/') >= 2}
        if seconds & GAME_TOP_DIRS:
            return 'Nested one folder too deep. The game only looks one level down, so nothing in it loads.'
    if not game_files:
        return 'No game files inside, so there is nothing for the game to load.'
    return None


# ----------------------------------------------------------------------------- vanilla game files

class Vanilla:
    """Extracts (and caches per build) the game's own versions of the files mods touch."""

    def __init__(self, HGPAKFile, game_dir, buildid):
        self.HG = HGPAKFile
        self.pcbanks = game_dir / 'GAMEDATA' / 'PCBANKS'
        self.dir = DATA / 'cache' / str(buildid) / 'vanilla'
        self.index_file = self.dir.parent / 'index.json'
        self.index = load_json(self.index_file, {'found': {}, 'missing': [], 'samples': {}})

    def paks(self):
        return [p for p in sorted(self.pcbanks.glob('*.pak')) if 'audio' not in p.name.lower()]

    def fetch(self, keys):
        """Extract game files by lower-case game path. Returns {key: local path} for those that exist."""
        missing = set(self.index['missing'])
        want = {k for k in keys if k not in self.index['found'] and k not in missing}
        for pak in self.paks():
            if not want:
                break
            names = {}
            for key in want:
                if key.startswith('globals/'):   # the globals pak stores bare file names
                    if pak.name.lower() == 'nmsarc.globals.pak':
                        names[key[len('globals/'):]] = key
                else:
                    names[key] = key
            if not names:
                continue
            try:
                result = self.HG(pak).extract_specific(list(names), as_buffer=False)
            except Exception:
                continue
            for inner, blob in result.items():
                if blob and inner in names:
                    key = names[inner]
                    out = self.dir / key
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(blob if not key.endswith('.dds') else b'')
                    self.index['found'][key] = pak.name
                    want.discard(key)
        if want:
            self.index['missing'] = sorted(missing | want)
        save_json(self.index_file, self.index)
        return {k: self.dir / k for k in keys if k in self.index['found']}

    def sample_headers(self, suffixes):
        """MBIN header of one game file per suffix (e.g. '.scene.mbin') for mods' custom files."""
        samples = self.index.setdefault('samples', {})
        need = [s for s in suffixes if s not in samples]
        for pak in self.paks():
            if not need:
                break
            try:
                with self.HG(pak) as p:
                    names = p.filenames
            except Exception:
                continue
            picks = {}
            for s in need:
                hit = next((n for n in names if n.endswith(s)), None)
                if hit:
                    picks[s] = hit
            if not picks:
                continue
            try:
                result = self.HG(pak).extract_specific(list(picks.values()), as_buffer=False)
            except Exception:
                continue
            for s, name in picks.items():
                hdr = read_header(result.get(name) or b'')
                if hdr:
                    samples[s] = hdr
                    need.remove(s)
        for s in need:
            samples[s] = None
        save_json(self.index_file, self.index)
        return samples


def decompile(mbinc_exe, mbin_paths):
    todo = [p for p in mbin_paths if p.suffix.lower() == '.mbin' and not p.with_suffix('.MXML').exists()]
    if not todo:
        return
    print(f'  decompiling {len(todo)} game files with MBINCompiler (only needed once per game build) ...')

    def run(p):
        subprocess.run([mbinc_exe, str(p)], capture_output=True, timeout=180,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, todo))


# ----------------------------------------------------------------------------- xml field checks

def is_item(prop):
    return prop.get('name') is None or '_index' in prop.attrib or '_id' in prop.attrib


def child_type(prop):
    return norm_type(prop.get('value'))


def collect_fields(node, cur_type, fields):
    for prop in node:
        if prop.tag != 'Property':
            continue
        if not is_item(prop) and cur_type:
            fields[cur_type].add(prop.get('name'))
        if len(prop):
            collect_fields(prop, child_type(prop), fields)


def check_fields(node, cur_type, fields, stats):
    for prop in node:
        if prop.tag != 'Property':
            continue
        if not is_item(prop):
            name = prop.get('name')
            if cur_type in fields:
                if name not in fields[cur_type] and name not in IGNORED_FIELDS:
                    stats['unknown'].append(f'{cur_type}.{name}')
                    continue   # nothing below a field that no longer exists can apply
                if not len(prop):
                    stats['ok'] += 1
            elif not len(prop):
                stats['unverified'] += 1
        if len(prop):
            check_fields(prop, child_type(prop), fields, stats)


def ids_by_list(root):
    out = collections.defaultdict(set)

    def rec(node, trail):
        for prop in node:
            if prop.tag != 'Property':
                continue
            if '_id' in prop.attrib:
                out[trail].add(prop.get('_id'))
            rec(prop, trail if is_item(prop) else f"{trail}/{prop.get('name') or ''}")
    rec(root, '')
    return out


def table_drift(mod_root, game_root):
    """Lists where the mod carries (most of) a full copy of a game table that has since grown."""
    found = []
    mod_ids, game_ids = ids_by_list(mod_root), ids_by_list(game_root)
    for trail, gids in game_ids.items():
        mids = mod_ids.get(trail, set())
        if len(gids) < 5 or not mids:
            continue
        if len(mids & gids) / len(gids) >= 0.6:
            new = sorted(gids - mids)
            if new:
                found.append({'list': trail.strip('/') or 'root', 'total': len(gids), 'new': len(new),
                              'examples': new[:4]})
    return found


class FieldIndex:
    def __init__(self, vanilla_dir):
        self.cache_file = vanilla_dir.parent / 'fields.json'
        mxml = sorted(p for p in vanilla_dir.rglob('*') if p.suffix.lower() == '.mxml')
        rels = [p.relative_to(vanilla_dir).as_posix().lower() for p in mxml]
        cached = load_json(self.cache_file, {})
        if cached.get('files') == rels:
            self.fields = {k: set(v) for k, v in cached['fields'].items()}
            self.roots = cached['roots']
        else:
            fields, roots = collections.defaultdict(set), {}
            for p, rel in zip(mxml, rels):
                root = parse_xml(p)
                if root is None:
                    continue
                rt = norm_type(root.get('template'))
                roots[rel] = rt
                collect_fields(root, rt, fields)
            self.fields, self.roots = dict(fields), roots
            save_json(self.cache_file, {'files': rels, 'roots': roots,
                                        'fields': {k: sorted(v) for k, v in self.fields.items()}})


# ----------------------------------------------------------------------------- nexus

def gql(query):
    d = http_json(NEXUS_GQL, {'query': query}, timeout=90)
    if d.get('errors'):
        raise RuntimeError(json.dumps(d['errors'])[:300])
    return d['data']


def nexus_lookup(ids):
    info, files = {}, {}
    ids = sorted(set(ids))
    for i in range(0, len(ids), 50):
        part = ids[i:i + 50]
        q = ('query { m: legacyModsByDomain(ids: [%s], count: 100) { nodes { modId name version updatedAt status author } } }'
             % ','.join('{gameDomain:"%s", modId:%d}' % (NEXUS_DOMAIN, x) for x in part))
        for node in gql(q)['m']['nodes']:
            info[node['modId']] = node
    for i in range(0, len(ids), 15):
        part = ids[i:i + 15]
        q = 'query { ' + ' '.join(
            'f%d: modFiles(modId: %d, gameId: %d) { fileId name version category date }' % (x, x, NEXUS_GAME_ID)
            for x in part) + ' }'
        for key, val in gql(q).items():
            files[int(key[1:])] = val or []
    return info, files


def nexus_search(name):
    term = re.sub(r'[^A-Za-z ]+', ' ', name)
    term = re.sub(r'\b(v|ver|version|exml|mxml|mod|nms)\b', ' ', term, flags=re.I)
    term = ' '.join(term.split()[:4])
    if len(term) < 3:
        return []
    q = ('query { s: mods(filter:{gameDomainName:[{value:"%s"}], name:[{value:"%s", op:WILDCARD}]}, count:3) '
         '{ nodes { modId name author } } }' % (NEXUS_DOMAIN, term.replace('"', '')))
    try:
        return gql(q)['s']['nodes']
    except Exception:
        return []


def archive_dirs(path, sevenzip):
    names = []
    try:
        if path.suffix.lower() == '.zip':
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        elif sevenzip:
            out = subprocess.run([str(sevenzip), 'l', '-ba', '-slt', str(path)], capture_output=True, text=True,
                                 timeout=60, errors='replace').stdout
            names = [ln[7:].replace('\\', '/') for ln in out.splitlines() if ln.startswith('Path = ')]
    except Exception:
        return set()
    dirs = set()
    for n in names:
        parts = n.strip('/').split('/')
        dirs.update(p.lower() for p in parts[:-1][:3])
        if n.endswith('/'):
            dirs.update(p.lower() for p in parts[:3])
    return dirs


# Newer Nexus downloads: "Beyond Base Building 1096 7.00 2026-09-09T18-31Z L5bXM34O7.zip"
NEXUS_ARCHIVE_RE_2026 = re.compile(
    r'^(?P<name>.+?) (?P<id>\d{1,6}) (?P<ver>\S+) (?P<iso>\d{4}-\d{2}-\d{2}T\d{2}-\d{2})Z \w+(?: \(\d+\))?\.(?:zip|7z|rar)$',
    re.I)


def parse_archive_name(filename):
    """(nexus_id, upload unix time) from a Nexus download's file name, or None."""
    m = NEXUS_ARCHIVE_RE.match(filename)
    if m:
        return int(m['id']), int(m['ts'])
    m = NEXUS_ARCHIVE_RE_2026.match(filename)
    if m:
        uploaded = dt.datetime.strptime(m['iso'], '%Y-%m-%dT%H-%M').replace(tzinfo=dt.timezone.utc)
        return int(m['id']), int(uploaded.timestamp())
    return None


def auto_map(mods, mapping, download_dirs):
    """Map new mod folders to Nexus ids using Nexus-named archives in the download folders."""
    unmapped = {m['name'].lower(): m['name'] for m in mods.values() if m['name'] not in mapping}
    if not unmapped:
        return []
    sevenzip = find_7zip()
    added = []
    for d in download_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        archives = [(p, parse_archive_name(p.name)) for p in d.iterdir() if p.is_file()]
        archives = [(p, parsed) for p, parsed in archives if parsed]
        for archive, (nexus_id, uploaded) in sorted(archives, key=lambda a: a[0].stat().st_mtime, reverse=True):
            if not unmapped:
                break
            for folder in archive_dirs(archive, sevenzip) & set(unmapped):
                name = unmapped.pop(folder)
                mapping[name] = {'nexus_id': nexus_id, 'file_ts': uploaded, 'source': f'auto: {archive.name}'}
                added.append(name)
    return added


# ----------------------------------------------------------------------------- analysis

def fetch_game_files(mods, vanilla):
    mbin_keys, xml_keys, dds_keys, suffixes = set(), set(), set(), set()
    for mod in mods.values():
        for f in mod['files']:
            if f['kind'] == 'MBIN':
                mbin_keys.add(f['key'])
                if multi_suffix(f['rel']):
                    suffixes.add(multi_suffix(f['rel']))
            elif f['kind'] == 'XML':
                xml_keys.add(f['key'])
            elif f['kind'] == 'DDS':
                dds_keys.add(f['key'])
    game = vanilla.fetch(sorted(mbin_keys | xml_keys | dds_keys))
    samples = vanilla.sample_headers(sorted(suffixes))
    return game, samples


def check_mod(mod, game, samples, fidx):
    res = {'problems': [], 'warnings': [], 'notes': [], 'tech': 'ok', 'checked': 0}
    layout = layout_problem(mod)
    if layout:
        res['tech'] = 'dead'
        res['problems'].append(layout)
        return res

    bad_mbin, old_mbin, ok_mbin, unknown_mbin = [], [], 0, 0
    for f in (x for x in mod['files'] if x['kind'] == 'MBIN'):
        hdr = f.get('hdr')
        if not hdr or hdr['magic'] not in MBIN_MAGIC or hdr['mbinver'] != MBIN_VERSION:
            old_mbin.append(f['rel'])
            continue
        ref = None
        gp = game.get(f['key'])
        if gp and gp.exists() and gp.stat().st_size >= 0x20:
            with open(gp, 'rb') as fh:
                ref = read_header(fh.read(0x20))
            if ref and ref['namehash'] != hdr['namehash']:
                bad_mbin.append(f['rel'])
                continue
        elif multi_suffix(f['rel']) and samples.get(multi_suffix(f['rel'])):
            ref = samples[multi_suffix(f['rel'])]
            if ref['namehash'] != hdr['namehash']:
                ref = None
        if ref is None:
            unknown_mbin += 1
        elif ref['guid'] != hdr['guid']:
            bad_mbin.append(f['rel'])
        else:
            ok_mbin += 1
    res['checked'] += ok_mbin + len(bad_mbin) + len(old_mbin)
    if old_mbin:
        res['problems'].append(f'{len(old_mbin)} binary file(s) use a pre-2024 MBIN format the game no longer reads.')
    if bad_mbin:
        built = sorted({f['hdr']['built_with'] for f in mod['files'] if f['rel'] in bad_mbin and f.get('hdr')})
        res['problems'].append(
            f'{len(bad_mbin)} of {len(bad_mbin) + ok_mbin} binary file(s) were built for an older data layout '
            f'(MBINCompiler {", ".join(built)}); this game version changed them. Example: {bad_mbin[0]}')
    if ok_mbin and not bad_mbin:
        res['notes'].append(f'{ok_mbin} binary file(s) match this game version exactly.')

    for f in (x for x in mod['files'] if x['kind'] == 'XML'):
        root = parse_xml(f['path'])
        if root is None:
            res['warnings'].append(f"{f['rel']}: not valid XML, the game will likely skip it.")
            continue
        mod_type = norm_type(root.get('template'))
        game_rel = re.sub(r'\.mbin$', '.mxml', f['key'])
        game_type = fidx.roots.get(game_rel) if fidx else None
        if game_type and mod_type and game_type != mod_type:
            res['problems'].append(f"{f['rel']}: the game file now uses template {game_type}, the mod has {mod_type}.")
            continue
        if not fidx:
            continue
        stats = {'unknown': [], 'ok': 0, 'unverified': 0}
        check_fields(root, mod_type, fidx.fields, stats)
        res['checked'] += 1
        if stats['unknown']:
            names = sorted(set(stats['unknown']))
            if stats['ok'] == 0 and stats['unverified'] == 0:
                res['problems'].append(
                    f"{f['rel'].split('/')[-1]}: every setting it changes no longer exists in this game version "
                    f"({', '.join(names[:4])}), so it does nothing.")
            else:
                res['warnings'].append(
                    f"{f['rel'].split('/')[-1]}: {len(names)} setting(s) no longer exist ({', '.join(names[:4])}); "
                    f"the other {stats['ok']} still apply.")
        game_path = game.get(f['key'])
        game_mxml = game_path.with_suffix('.MXML') if game_path else None
        if game_mxml and game_mxml.exists():
            game_root = parse_xml(game_mxml)
            if game_root is not None:
                for d in table_drift(root, game_root):
                    res['warnings'].append(
                        f"{f['rel'].split('/')[-1]}: carries an older full copy of the {d['list']} list. "
                        f"The game now has {d['new']} entries the copy lacks (of {d['total']}, e.g. "
                        f"{', '.join(d['examples'])}). It can hide or undo things this update added.")

    dds = [f for f in mod['files'] if f['kind'] == 'DDS']
    if dds:
        custom = [f for f in dds if f['key'] not in game]
        replaced = len(dds) - len(custom)
        if replaced:
            res['notes'].append(f'{replaced} texture(s) replace game textures that still exist (textures rarely break).')
        if custom and not any(x['kind'] in ('MBIN', 'XML') for x in mod['files']):
            res['warnings'].append(f'{len(custom)} texture(s) have no matching game file any more.')
    if any(f.get('lua') for f in mod['files']):
        res['notes'].append('Includes its AMUMSS .lua script, so it can be rebuilt for a new game version with AMUMSS.')
    if unknown_mbin:
        res['notes'].append(f'{unknown_mbin} custom binary file(s) have no game counterpart to compare with.')

    if res['problems']:
        res['tech'] = 'broken'
    elif res['warnings']:
        res['tech'] = 'risky'
    elif res['checked'] == 0 and not dds:
        res['tech'] = 'untested'
    return res


def nexus_view(entry, info, files, release_ts, installed_at):
    if not entry or not entry.get('nexus_id'):
        return None
    mid = entry['nexus_id']
    node = info.get(mid) or {}
    fl = files.get(mid) or []
    active = [f for f in fl if f['category'] in ACTIVE_FILE_CATEGORIES]
    newest = max(active or fl, key=lambda f: f['date'], default=None)
    # Your copy dates from whichever is later: the upload time of the archive it came from, or when the
    # folder was created (a Vortex install or fresh extract of a newer version creates it anew).
    have = max(entry.get('file_ts') or 0, installed_at or 0)
    since_patch = sorted((f for f in fl if f['date'] >= release_ts and f['category'] != 'ARCHIVED'),
                         key=lambda f: -f['date'])
    pending = [f for f in since_patch if f['category'] in ACTIVE_FILE_CATEGORIES and f['date'] > have + 60]
    newer = [f for f in active if f['date'] > have + 60 and f['date'] < release_ts]
    return {'id': mid, 'name': node.get('name') or entry.get('note') or '', 'author': node.get('author') or '',
            'version': node.get('version') or '', 'updated': node.get('updatedAt', '')[:10],
            'status': node.get('status') or 'unknown', 'newest': newest, 'since_patch': since_patch,
            'pending': pending, 'newer_than_installed': newer, 'have': have,
            'url': f'https://www.nexusmods.com/{NEXUS_DOMAIN}/mods/{mid}?tab=files'}


ACTIONS = {
    'update':    (0, 'Get the update', 'act-update'),
    'fixed':     (1, 'Fix is out (disabled mod)', 'act-update'),
    'reenable':  (2, 'Works again (disabled mod)', 'act-optional'),
    'disable':   (3, 'Disable for now', 'act-disable'),
    'remove':    (4, 'Remove', 'act-remove'),
    'duplicate': (5, 'Remove duplicate', 'act-remove'),
    'partial':   (6, 'Partly outdated', 'act-partial'),
    'optional':  (7, 'Optional update', 'act-optional'),
    'ok':        (8, 'OK', 'act-ok'),
    'untested':  (9, 'Not testable', 'act-untested'),
    'parked':    (10, 'Still broken (disabled)', 'act-untested'),
    'delete':    (11, 'Safe to delete (disabled)', 'act-untested'),
}


def decide(tech, nx, duplicate_of, disabled):
    pending = bool(nx and nx['pending'])
    had_update = bool(nx and nx['since_patch'])
    # A newer main file posted before this game version (e.g. before a hotfix went live) is still worth grabbing.
    newer = bool(nx and any(f['category'] == 'MAIN' for f in nx['newer_than_installed']))
    if disabled:
        if duplicate_of:
            return 'delete', f'You have the newer copy "{duplicate_of}" in MODS, so this disabled one can be deleted.'
        if tech == 'dead':
            return 'delete', 'It cannot work in current No Man\'s Sky, so this disabled copy can be deleted.'
        if pending:
            return 'fixed', ('The author posted a version after this game update. Download it into MODS '
                             '(with Vortex: Mod Manager Download), then delete this disabled copy.')
        if tech in ('ok', 'untested'):
            return 'reenable', 'Compatible with this game version again. Move it back into MODS.'
        if tech == 'risky':
            return 'parked', 'Now only partly outdated (details below). Move it back into MODS if you can live with that.'
        return 'parked', 'Still broken with this game version and no fix posted yet. Leave it here.'
    if duplicate_of:
        return 'duplicate', f'Same mod as "{duplicate_of}", which is newer. Delete this older copy.'
    if tech == 'dead':
        return 'remove', f'It cannot work in current No Man\'s Sky. Delete the folder, or move it to {DISABLED_DIR}.'
    if tech == 'broken':
        if pending:
            return 'update', 'Broken by this game version, but the author already posted a fix. Download it.'
        if had_update:
            return 'disable', (f'Still broken even though you have the author\'s newest version. Move it to {DISABLED_DIR} '
                               '(the checker keeps watching it there).')
        return 'disable', (f'Broken by this game version and no fix posted yet. Move it to {DISABLED_DIR} '
                           '(the checker keeps watching it there) and re-check in a few days.')
    if tech == 'risky':
        if pending or newer:
            return 'update', 'Partly outdated, and the author already posted an update. Download it.'
        return 'partial', 'Mostly works, but parts are outdated (details below). Keep it only if you are fine with that.'
    if pending:
        return 'optional', 'Still compatible. The author posted an update after this game version came out, worth grabbing.'
    if newer:
        return 'optional', 'Still compatible, but Nexus has a newer version than your copy, worth grabbing.'
    if tech == 'untested':
        return 'untested', 'Nothing in it can be compared against the game files.'
    return 'ok', 'Compatible with this game version. Nothing to do.'


def find_duplicates(mods, mapping):
    """Copies of the same Nexus mod that overwrite the same files: keep the newest active one."""
    by_id = collections.defaultdict(list)
    for key, mod in mods.items():
        mid = (mapping.get(mod['name']) or {}).get('nexus_id')
        if mid:
            by_id[mid].append(key)
    dup = {}
    for keys in by_id.values():
        if len(keys) < 2:
            continue
        files = {k: {f['key'] for f in mods[k]['files'] if f['kind'] in ('MBIN', 'XML', 'DDS')} for k in keys}
        # Upload time of the installed file when known for every copy, else when the folder landed on disk.
        if all((mapping.get(mods[k]['name']) or {}).get('file_ts') for k in keys):
            age = lambda k: mapping[mods[k]['name']]['file_ts']  # noqa: E731
        else:
            age = lambda k: mods[k]['installed_at']  # noqa: E731
        keep = max([k for k in keys if not mods[k]['disabled']] or keys, key=age)
        for k in keys:
            if k != keep and files[k] & files[keep]:
                dup[k] = mods[keep]['name']
    return dup


def find_conflicts(mods, duplicates):
    """Binary files replaced by more than one active mod: only one copy can win."""
    owners = collections.defaultdict(set)
    for key, mod in mods.items():
        if key in duplicates or mod['disabled']:
            continue
        for f in mod['files']:
            if f['kind'] == 'MBIN':
                owners[f['key']].add(key)
    conflicts = collections.defaultdict(lambda: collections.defaultdict(int))
    for names in owners.values():
        if len(names) > 1:
            for n in names:
                for other in names - {n}:
                    conflicts[n][mods[other]['name']] += 1
    return conflicts


# ----------------------------------------------------------------------------- report

CSS = """
:root { --bg:#f7f7f5; --card:#ffffff; --fg:#1d1d1f; --muted:#6b6b70; --line:#e3e3e0; --link:#1a5fb4;
  --update:#c2410c; --disable:#b91c1c; --remove:#7f1d1d; --partial:#a16207; --optional:#1d4ed8; --ok:#15803d; --untested:#6b7280; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --card:#1e1e22; --fg:#ececf0; --muted:#9a9aa3; --line:#2e2e34;
  --link:#8ab4f8; --update:#fb923c; --disable:#f87171; --remove:#fca5a5; --partial:#facc15; --optional:#93c5fd; --ok:#4ade80; --untested:#9ca3af; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
main { max-width:1180px; margin:0 auto; padding-block:24px 60px; padding-inline:16px; }
h1 { font-size:22px; margin:0 0 4px; } .sub { color:var(--muted); margin:0 0 18px; }
.meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:10px; margin-bottom:18px; }
.meta div { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.meta b { display:block; font-size:12px; color:var(--muted); font-weight:600; }
.chips { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 14px; }
.chip { border:1px solid var(--line); background:var(--card); border-radius:999px; padding:4px 12px; cursor:pointer; font:inherit; color:inherit; }
.chip[aria-pressed=true] { outline:2px solid var(--link); }
.wrap { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; min-width:760px; }
th, td { text-align:left; vertical-align:top; padding:9px 12px; border-bottom:1px solid var(--line); }
th { font-size:12px; color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--card); }
tr:last-child td { border-bottom:0; }
tr.parked td { opacity:.8; }
.badge { display:inline-block; font-weight:600; font-size:12px; padding:2px 8px; border-radius:6px; border:1px solid currentColor; white-space:nowrap; }
.act-update { color:var(--update); } .act-disable { color:var(--disable); } .act-remove { color:var(--remove); }
.act-partial { color:var(--partial); } .act-optional { color:var(--optional); } .act-ok { color:var(--ok); } .act-untested { color:var(--untested); }
.mod { font-weight:600; } .muted { color:var(--muted); font-size:12px; }
a { color:var(--link); } details summary { cursor:pointer; color:var(--muted); font-size:12px; }
ul { margin:6px 0 0; padding-left:18px; } li { margin:2px 0; }
.p { color:var(--disable); } .w { color:var(--partial); }
footer { color:var(--muted); font-size:12px; margin-top:16px; }
"""

JS = """
const chips = document.querySelectorAll('.chip');
chips.forEach(c => c.addEventListener('click', () => {
  const on = c.getAttribute('aria-pressed') !== 'true';
  chips.forEach(x => x.setAttribute('aria-pressed', 'false'));
  if (on) c.setAttribute('aria-pressed', 'true');
  document.querySelectorAll('tbody tr').forEach(r => { r.hidden = on && r.dataset.act !== c.dataset.act; });
}));
"""


def nexus_cell(r):
    e = html.escape
    nx = r['nexus']
    if not nx:
        out = f'<span class="muted">{e(r["mapping_note"] or "Not linked to a Nexus page (add it in mods.json)")}</span>'
        if r['suggestions']:
            out += '<div class="muted">Maybe: ' + ', '.join(
                f'<a href="https://www.nexusmods.com/{NEXUS_DOMAIN}/mods/{s["modId"]}" target="_blank" rel="noopener">'
                f'{e(s["name"])} (#{s["modId"]})</a>' for s in r['suggestions']) + '</div>'
        return out
    out = f'<a href="{e(nx["url"])}" target="_blank" rel="noopener">{e(nx["name"] or "#" + str(nx["id"]))}</a>'
    out += f'<div class="muted">latest v{e(nx["version"])}, updated {e(nx["updated"])}'
    if nx['status'] not in ('published', 'unknown'):
        out += f', status: {e(nx["status"])}'
    out += '</div>'
    if nx['pending']:
        f0 = nx['pending'][0]
        out += (f'<div class="muted"><b>{len(nx["pending"])} new file(s) since this game version</b>, '
                f'newest: {e(f0["name"])} v{e(str(f0["version"]))} ({fmt_date(f0["date"])})</div>')
    elif nx['since_patch']:
        out += '<div class="muted">Your copy is newer than the files posted since this game version.</div>'
    elif nx['newer_than_installed']:
        out += (f'<div class="muted">{len(nx["newer_than_installed"])} newer file(s) than your copy '
                '(from before this game version)</div>')
    return out


def build_report(rows, ctx):
    e = html.escape
    counts = collections.Counter(r['action'] for r in rows)
    chips = ''.join(
        f'<button class="chip {ACTIONS[a][2]}" data-act="{a}" aria-pressed="false">{e(ACTIONS[a][1])}: {counts[a]}</button>'
        for a in sorted(counts, key=lambda a: ACTIONS[a][0]))
    body = []
    for r in rows:
        _order, label, cls = ACTIONS[r['action']]
        items = ''.join(f'<li class="p">{e(x)}</li>' for x in r['tech']['problems'])
        items += ''.join(f'<li class="w">{e(x)}</li>' for x in r['tech']['warnings'])
        items += ''.join(f'<li>{e(x)}</li>' for x in r['tech']['notes'])
        items += ''.join(f'<li>{e(x)}</li>' for x in r['extra'])
        details = f'<details><summary>details</summary><ul>{items}</ul></details>' if items else ''
        src = '<b>Vortex</b>' if r['vortex'] else '<b>Web</b>'
        if r['disabled']:
            where = f'<div class="muted">in {DISABLED_DIR}</div>'
        elif r['ingame'] is None:
            where = f'<div class="muted">{src}</div>'
        else:
            where = f'<div class="muted">{src}, {"on" if r["ingame"]["enabled"] else "off"} in game</div>'
        row_cls = ' class="parked"' if r['disabled'] else ''
        body.append(
            f'<tr data-act="{r["action"]}"{row_cls}><td><div class="mod">{e(r["folder"])}</div>{where}</td>'
            f'<td><span class="badge {cls}">{e(label)}</span></td>'
            f'<td>{e(r["advice"])}{details}</td><td>{nexus_cell(r)}</td></tr>')
    m = ctx
    mb = m['mbinc']
    mb_text = (f'MBINCompiler {e(mb["tag"])} (tested on build {mb["tested_build"]})' if mb else 'not available (XML checks skipped)')
    active = sum(not r['disabled'] for r in rows)
    parked = len(rows) - active
    sub = f'{active} mods in {e(str(m["mods_dir"]))}'
    if parked:
        sub += f' and {parked} parked in {DISABLED_DIR}'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NMS Mod Check</title>
<style>{CSS}</style></head><body>
<main>
<h1>NMS Mod Check</h1>
<p class="sub">{sub}, checked {e(m['now'])}</p>
<div class="meta">
<div><b>Your game</b>Steam build {m['buildid']}{' (current public build)' if m['is_public'] else ''}, installed {fmt_date(m['lastupdated'])}</div>
<div><b>This game version went live</b>{fmt_date(m['release_ts'])}. Nexus files uploaded after this count as "since this game version".</div>
<div><b>Game data format</b>{mb_text}</div>
<div><b>Checks</b>binary headers vs game files, XML fields vs game data, Nexus files. Filter by clicking a count.</div>
</div>
<div class="chips">{chips}</div>
<div class="wrap"><table>
<thead><tr><th>Mod folder</th><th>Status</th><th>What to do</th><th>Nexus</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div>
<footer>Generated by nms_mod_check.py. Mods marked Vortex are managed by Vortex: update, disable and remove them there.
To disable a Web mod, move its folder into GAMEDATA\\{DISABLED_DIR} (the checker keeps watching it there) or set Enabled
to false in Binaries\\SETTINGS\\GCMODSETTINGS.MXML. Nothing was changed by this check.</footer>
</main>
<script>{JS}</script>
</body></html>
"""


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--no-open', action='store_true', help="don't open the report in the browser")
    ap.add_argument('--offline', action='store_true', help='skip Nexus and GitHub lookups')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    cfg = load_json(CONFIG_FILE, {})
    game_dir = find_game_dir(cfg)
    mods_dir = game_dir / 'GAMEDATA' / 'MODS'
    man = read_manifest(game_dir)
    print(f'Game: {game_dir}  (Steam build {man["buildid"]})')

    public = None if args.offline else steam_public_build()
    is_public = bool(public and public['buildid'] == man['buildid'])
    release_ts = public['timeupdated'] if is_public else man['lastupdated']

    mods = scan_mods(mods_dir)
    active = len(mods)
    mods.update(scan_mods(game_dir / 'GAMEDATA' / DISABLED_DIR, disabled=True))
    parked = len(mods) - active
    deployed = vortex_folders(mods_dir)
    for mod in mods.values():
        mod['vortex'] = not mod['disabled'] and mod['name'].lower() in deployed
        if mod['vortex']:
            # Vortex often deploys into an existing folder, whose dates then miss the update: use the file's upload time.
            mod['installed_at'] = max(mod['installed_at'], deployed[mod['name'].lower()])
    print(f'Found {active} mod folders' + (f' and {parked} parked in {DISABLED_DIR}' if parked else '') + '.')

    mapping_doc = load_json(MAPPING_FILE, {'_help': MAPPING_HELP, 'mods': {}})
    mapping = mapping_doc.setdefault('mods', {})
    default_dirs = [Path.home() / 'Downloads', Path(os.environ.get('APPDATA') or HERE) / 'Vortex' / 'downloads' / NEXUS_DOMAIN]
    added = auto_map(mods, mapping, cfg.get('download_dirs') or [str(d) for d in default_dirs])
    if added:
        print(f'  linked {len(added)} new folder(s) to Nexus from your downloads: {", ".join(added)}')
        save_json(MAPPING_FILE, mapping_doc)

    HGPAKFile = ensure_hgpaktool()
    mbinc = get_mbincompiler(man['buildid'], args.offline)
    vanilla = Vanilla(HGPAKFile, game_dir, man['buildid'])
    print('Reading the matching game files ...')
    game, samples = fetch_game_files(mods, vanilla)
    fidx = None
    if mbinc:
        decompile(mbinc['exe'], [p for k, p in game.items() if k.endswith('.mbin')])
        fidx = FieldIndex(vanilla.dir)

    info, files = {}, {}
    if not args.offline:
        ids = [(mapping.get(m['name']) or {}).get('nexus_id') for m in mods.values()]
        ids = [i for i in ids if i]
        print(f'Asking Nexus about {len(set(ids))} mods ...')
        try:
            info, files = nexus_lookup(ids)
        except Exception as ex:
            print(f'  ! Nexus lookup failed: {ex}')

    settings, disable_all = read_mod_settings(game_dir)
    duplicates = find_duplicates(mods, mapping)
    conflicts = find_conflicts(mods, duplicates)

    rows = []
    for key, mod in mods.items():
        name = mod['name']
        entry = mapping.get(name) or {}
        tech = check_mod(mod, game, samples, fidx)
        bad = entry.get('known_bad')
        if bad and mod['installed_at'] < dt.datetime.fromisoformat(bad['found']).timestamp() + 86400:
            tech['tech'] = 'broken'   # found broken by playing, whatever the file checks say
            tech['problems'].insert(0, f"Tested in game: {bad['note']} It stays marked broken until a newer copy is installed.")
        nx = nexus_view(entry, info, files, release_ts, mod['installed_at'])
        action, advice = decide(tech['tech'], nx, duplicates.get(key), mod['disabled'])
        if not mod['files']:
            action = 'delete' if mod['disabled'] else 'remove'
            advice = ('Empty folder: only leftover Vortex marker files, so nothing in it loads (usually a mod that was '
                      'turned off or removed in Vortex). Safe to delete.')
        elif not mod['disabled'] and action in ('update', 'optional'):
            advice += (' Vortex mod: download the update and pick Replace when Vortex asks.' if mod['vortex'] else
                       ' Web copy: delete this folder first, then use Mod Manager Download on Nexus.')
        elif mod['vortex'] and action in ('disable', 'remove', 'duplicate'):
            advice = {'disable': 'Broken by this game version. It is a Vortex mod, so turn it off in Vortex for now '
                                 '(Vortex shows when an update is out).',
                      'remove': 'It cannot work in current No Man\'s Sky. It is a Vortex mod, so remove it in Vortex.',
                      'duplicate': f'Same mod as "{duplicates.get(key)}", which is newer. It is a Vortex mod, '
                                   'so remove this copy in Vortex.'}[action]
        extra =[f'Replaces {n} of the same binary file(s) as "{other}": only one copy takes effect '
                 f'(the mod with the lower ModPriority in GCMODSETTINGS.MXML wins).'
                 for other, n in sorted(conflicts.get(key, {}).items())]
        suggestions = [] if (entry.get('nexus_id') or args.offline or entry.get('note')) else nexus_search(name)
        rows.append({'folder': name, 'disabled': mod['disabled'], 'vortex': mod['vortex'], 'action': action, 'advice': advice, 'tech': tech,
                     'nexus': nx, 'extra': extra, 'suggestions': suggestions, 'mapping_note': entry.get('note'),
                     'ingame': None if mod['disabled'] else settings.get(name.upper())})
    rows.sort(key=lambda r: (ACTIONS[r['action']][0], r['folder'].lower()))

    ctx = {'mods_dir': mods_dir, 'now': dt.datetime.now().strftime('%Y-%m-%d %H:%M'), 'buildid': man['buildid'],
           'lastupdated': man['lastupdated'], 'release_ts': release_ts, 'is_public': is_public, 'mbinc': mbinc}
    REPORT_FILE.write_text(build_report(rows, ctx), encoding='utf-8')
    save_json(DATA / 'last_check.json', rows)

    print()
    for action in sorted({r['action'] for r in rows}, key=lambda a: ACTIONS[a][0]):
        group = [r['folder'] for r in rows if r['action'] == action]
        print(f'{ACTIONS[action][1]} ({len(group)}): {", ".join(group)}')
    if disable_all:
        print('\nNote: "DisableAllMods" is on in the game settings, so no mods load at all right now.')
    print(f'\nReport: {REPORT_FILE}')
    if not args.no_open:
        webbrowser.open(REPORT_FILE.as_uri())


if __name__ == '__main__':
    main()
