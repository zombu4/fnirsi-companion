"""Releases on GitHub of the two firmware repositories and of this program: the list, the choice of the one file to
install, the download into the cache and its SHA-256 check.  Standard library only.

    repository                                  kind       tag     the file to install
    zombu4/fnirsi-lc1020e-firmware-rewrite      LC1020E    vA.B.C  APP_OWN.uf2
    zombu4/fnirsi-2c53t-firmware-rewrite        2C53T      vA.B.C  APP_2C53T_V<A.B.C>_<yymmdd>.bin
    zombu4/fnirsi-companion                     companion  vA.B.C  none (installers; the release page is opened)

Only these files are ever chosen (DESIGN 13.10 / 13.17).  STARTER.bin, BOOT_*.bin, swd_loader.bin and APP_OWN.bin are
never offered, so the program cannot install a bootloader or a starter from here.  Each release carries SHA256SUMS
(`sha256sum` format); a downloaded file is accepted only when its SHA-256 is the one listed there for its name and its
length is the length the API lists for it.  A verified file is kept in the cache folder with a side file holding the
verified hash; a later request recomputes the hash of the cached file and does not use the network when it matches.

How a meter's firmware text relates to a release:
  2C53T     INFO text "A.B.C" (digits and dots, zero padded) is the About text, and the release tag is "v" + that text
            (the 2C53T repository's build.yml refuses a tag that differs from it), so newer / same / older is exact.
  LC1020E   INFO text "LC1020E V1.1 OWN" carries no release version: its V1.1 is not the tag (release v1.0.0 exists
            with that text).  The relation is "unknown", unless the caller passes the tag this program itself installed
            on that meter (settings.installed_release), which is then compared with the release tags.
Lists come from the GitHub API, `get(url) -> bytes` being replaceable so tests need no network.
"""
import hashlib
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import paths

REPOS = {'LC1020E': 'zombu4/fnirsi-lc1020e-firmware-rewrite',
         '2C53T': 'zombu4/fnirsi-2c53t-firmware-rewrite',
         'companion': 'zombu4/fnirsi-companion'}
API = 'https://api.github.com/repos/%s/releases?per_page=%d&page=%d'
ASSET_URL = 'https://github.com/%s/releases/download/%s/%s'
PAGE_URL = 'https://github.com/%s/releases/tag/%s'
TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)')
LC_IMAGE = 'APP_OWN.uf2'
C2_IMAGE = re.compile(r'APP_2C53T_V(\d+)\.(\d+)\.(\d+)_\d{6}\.bin')
SUMS = 'SHA256SUMS'
SUMS_LINE = re.compile(r'([0-9a-fA-F]{64}) [ *](\S.*)')
VERSION_TEXT = re.compile(r'(\d+)\.(\d+)\.(\d+)')
TIMEOUT = 20
PER_PAGE, MAX_PAGES = 100, 5                    # the list is fetched page by page, at most 500 releases
CACHE_SECONDS = 30.0                            # the questions of one release check cost one fetch per repository
MAX_BYTES = 8 * 1024 * 1024                     # no file of the releases is larger (the 2C53T image is 0xE0000 at most)


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    kind: str                   # key of REPOS
    version: tuple              # (A, B, C)
    tag: str
    text: str                   # "A.B.C"
    published: str              # "YYYY-MM-DD" or ""
    assets: dict                # file name -> size in bytes, as the API lists them

    @property
    def page_url(self):
        return PAGE_URL % (REPOS[self.kind], self.tag)


def _ssl_context():
    """The system's certificates plus certifi's CA list when certifi is installed.  A python.org Python on macOS does not
    use the keychain: without a CA file of its own every download fails with "certificate verify failed"."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return ctx


def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'FNIRSI-Companion', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ReleaseError('%s: more than %d bytes' % (url, MAX_BYTES))
    return data


_cache = {}                                     # kind -> (monotonic time, [Release])


def forget():
    """Drop the kept lists (the next question fetches again)."""
    _cache.clear()


def parse_releases(kind, api_list):
    """The Releases of `kind` in an API answer, newest first.  Drafts, pre-releases and tags that are not vA.B.C are left
    out."""
    out = []
    for r in api_list:
        if not isinstance(r, dict) or r.get('draft') or r.get('prerelease'):
            continue
        tag = r.get('tag_name')
        m = TAG.fullmatch(tag) if isinstance(tag, str) else None
        if not m:
            continue
        v = tuple(int(x) for x in m.groups())
        assets = {a['name']: a.get('size') for a in r.get('assets') or []
                  if isinstance(a, dict) and isinstance(a.get('name'), str)}
        out.append(Release(kind, v, tag, '%d.%d.%d' % v, str(r.get('published_at') or '')[:10], assets))
    out.sort(key=lambda e: e.version, reverse=True)
    return out


def list_releases(kind, get=None):
    """The published releases of `kind` ('LC1020E', '2C53T', 'companion'), newest first.  With `get` given the list is
    fetched every time, else kept for CACHE_SECONDS."""
    if kind not in REPOS:
        raise ReleaseError('unknown kind %r' % (kind,))
    if get is None:
        hit = _cache.get(kind)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    fetch = get or _get
    raw = []
    for page in range(1, MAX_PAGES + 1):
        try:
            part = json.loads(fetch(API % (REPOS[kind], PER_PAGE, page)))
        except ValueError as e:
            raise ReleaseError('the release list of %s is not JSON: %s' % (REPOS[kind], e))
        if not isinstance(part, list):
            raise ReleaseError('the release list of %s is not a list' % REPOS[kind])
        raw.extend(part)
        if len(part) < PER_PAGE:
            break
    found = parse_releases(kind, raw)
    if get is None:
        _cache[kind] = (time.monotonic(), found)
    return found


def image_name(release):
    """The name of the one file of `release` that is installed on the meter; ReleaseError when there is none, or more
    than one, or (2C53T) its version differs from the tag."""
    if release.kind == 'LC1020E':
        names = [n for n in release.assets if n == LC_IMAGE]
    elif release.kind == '2C53T':
        names = [n for n in release.assets if C2_IMAGE.fullmatch(n)]
    else:
        raise ReleaseError('the program\'s releases carry installers, no meter image')
    if len(names) != 1:
        raise ReleaseError('%s: %d image files of the expected kind, 1 needed' % (release.tag, len(names)))
    if release.kind == '2C53T':
        v = tuple(int(x) for x in C2_IMAGE.fullmatch(names[0]).groups())
        if v != release.version:
            raise ReleaseError('%s: the file %s is of another version than the tag' % (release.tag, names[0]))
    if SUMS not in release.assets:
        raise ReleaseError('%s has no %s' % (release.tag, SUMS))
    return names[0]


def parse_sums(text):
    """{name: sha256 hex lowercase} of a `sha256sum` listing; ReleaseError for a line of another shape or for one name
    listed with two different hashes."""
    out = {}
    for ln in text.splitlines():
        if not ln.strip():
            continue
        m = SUMS_LINE.fullmatch(ln.rstrip('\r'))
        if not m:
            raise ReleaseError('%s: line not in sha256sum format' % SUMS)
        h, name = m.group(1).lower(), m.group(2)
        if out.setdefault(name, h) != h:
            raise ReleaseError('%s: %s listed with two different hashes' % (SUMS, name))
    return out


def _sums(release, get):
    url = ASSET_URL % (REPOS[release.kind], release.tag, SUMS)
    return parse_sums(get(url).decode('utf-8', 'replace'))


def meter_version(kind, text):
    """(A, B, C) from the 2C53T's INFO text (bytes or str, padded with zero bytes / spaces), None when it is not "N.N.N".
    None for the LC1020E and for anything else: its text carries no release version."""
    if kind != '2C53T':
        return None
    if isinstance(text, bytes):
        text = text.decode('ascii', 'replace')
    m = VERSION_TEXT.fullmatch(text.strip('\x00 '))
    return tuple(int(x) for x in m.groups()) if m else None


def relation(kind, meter_text, release, installed_tag=None):
    """How `release` stands to the meter: 'newer' (the release is newer), 'same', 'older', or 'unknown'.
    2C53T / companion: `meter_text` is the INFO text / the program's own "A.B.C".  LC1020E: only `installed_tag`, the
    tag the program installed itself, can tell (see the module text); otherwise 'unknown'."""
    if kind == 'companion':
        m = VERSION_TEXT.fullmatch(meter_text or '')
        have = tuple(int(x) for x in m.groups()) if m else None
    elif kind == '2C53T':
        have = meter_version(kind, meter_text or '')
    else:
        m = TAG.fullmatch(installed_tag or '')
        have = tuple(int(x) for x in m.groups()) if m else None
    if have is None:
        return 'unknown'
    return 'newer' if release.version > have else 'older' if release.version < have else 'same'


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def download(release, get=None, cache=None):
    """Path of the verified image file of `release` in the cache, downloading it when it is not there; ReleaseError when
    the length or the SHA-256 is not the published one (nothing is kept then)."""
    fetch = get or _get
    name = image_name(release)
    folder = os.path.join(cache or paths.cache_dir(), 'releases', release.kind, release.tag)
    path = os.path.join(folder, name)
    try:
        with open(path + '.sha256', encoding='ascii') as f:
            kept = f.read().strip()
        if os.path.isfile(path) and kept == _sha256_file(path):
            return path
    except OSError:
        pass
    want = _sums(release, fetch).get(name)
    if want is None:
        raise ReleaseError('%s lists no hash for %s' % (SUMS, name))
    blob = fetch(ASSET_URL % (REPOS[release.kind], release.tag, urllib.parse.quote(name)))
    if release.assets[name] is not None and len(blob) != release.assets[name]:
        raise ReleaseError('%s: %d bytes, the release lists %d' % (name, len(blob), release.assets[name]))
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        raise ReleaseError('%s: SHA-256 %s does not match the published %s' % (name, got, want))
    os.makedirs(folder, exist_ok=True)
    for p, data in ((path, blob), (path + '.sha256', (got + '\n').encode('ascii'))):
        with open(p + '.part', 'wb') as f:
            f.write(data)
        os.replace(p + '.part', p)
    return path


def known_image_hashes(kind, get=None):
    """{sha256: (tag, file name)} of the image files in the SHA256SUMS of every release of `kind` (DESIGN 13.26: an image
    not in it gets a warning).  A release without SHA256SUMS or without the image file adds nothing."""
    fetch = get or _get
    out = {}
    for rel in list_releases(kind, get):
        try:
            name = image_name(rel)
        except ReleaseError:
            continue
        h = _sums(rel, fetch).get(name)
        if h:
            out[h] = (rel.tag, name)
    return out


def listed_hashes(kind, get=None):
    """{sha256: (tag, file name)} of every line of the SHA256SUMS of every release of `kind`, whatever the file is.
    Used for the LC1020E bootloader slot files (DESIGN 13.26): no release names a slot file kind of its own (release
    v1.0.0 lists APP_OWN.uf2, APP_OWN.bin and STARTER.bin), so a slot file counts as published only when its SHA-256
    is listed there under any name; the device module's slot check still refuses a file that is not a slot file for
    the free slot.  A release without SHA256SUMS adds nothing."""
    fetch = get or _get
    out = {}
    for rel in list_releases(kind, get):
        if SUMS not in rel.assets:
            continue
        for name, h in _sums(rel, fetch).items():
            out.setdefault(h, (rel.tag, name))
    return out
