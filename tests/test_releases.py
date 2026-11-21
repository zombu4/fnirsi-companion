"""Tests of core/releases.py with GitHub API answers saved in tests/data/ (no network: `get` is replaced)."""
import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fnirsi_companion.core import releases as rel

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


def saved(name):
    with open(os.path.join(DATA, name), 'rb') as f:
        return f.read()


def image_bytes(size, seed):
    return (bytes([seed]) * size)[:size]


class FakeGitHub:
    """Answers the API list and the asset downloads of the saved releases; images are made up with the listed sizes."""

    def __init__(self):
        self.calls = []
        self.files = {}                                 # url -> bytes
        self.api = {'LC1020E': saved('api_releases_lc1020e.json'), '2C53T': saved('api_releases_2c53t.json')}
        for kind, raw in self.api.items():
            for r in json.loads(raw):
                lines = []
                for a in r['assets']:
                    if a['name'] == rel.SUMS:
                        continue
                    blob = image_bytes(a['size'], len(a['name']) + len(r['tag_name']))
                    self.files[rel.ASSET_URL % (rel.REPOS[kind], r['tag_name'], a['name'])] = blob
                    lines.append('%s  %s\n' % (hashlib.sha256(blob).hexdigest(), a['name']))
                self.files[rel.ASSET_URL % (rel.REPOS[kind], r['tag_name'], rel.SUMS)] = ''.join(lines).encode()

    def __call__(self, url):
        self.calls.append(url)
        for kind, raw in self.api.items():
            if url.startswith('https://api.github.com/repos/%s/releases' % rel.REPOS[kind]):
                return raw if '&page=1' in url else b'[]'
        if url.startswith('https://api.github.com/'):
            return b'[]'
        return self.files[url]


class ListTest(unittest.TestCase):
    def test_saved_answers(self):
        gh = FakeGitHub()
        lc = rel.list_releases('LC1020E', gh)
        self.assertEqual([r.tag for r in lc], ['v1.0.0'])
        self.assertEqual((lc[0].version, lc[0].text, lc[0].published), ((1, 0, 0), '1.0.0', '2026-11-21'))
        self.assertEqual(lc[0].page_url, 'https://github.com/zombu4/fnirsi-lc1020e-firmware-rewrite/releases/tag/v1.0.0')
        c2 = rel.list_releases('2C53T', gh)
        self.assertEqual([r.tag for r in c2], ['v1.4.17', 'v1.4.16'])
        self.assertEqual(c2[0].assets['APP_2C53T_V1.4.17_261119.bin'], 561716)
        self.assertEqual(rel.list_releases('companion', gh), [])

    def test_skips_and_sorts(self):
        api = [{'tag_name': 'v1.2.0'}, {'tag_name': 'v1.10.0'}, {'tag_name': 'v1.9.0'}, {'tag_name': 'v2.0.0', 'draft': True},
               {'tag_name': 'v3.0.0', 'prerelease': True}, {'tag_name': 'bl-v1.0.0'}, {'tag_name': 'v1.0'},
               {'tag_name': 'v1.0.0a'}, {'tag_name': None}, 'junk']
        self.assertEqual([r.tag for r in rel.parse_releases('2C53T', api)], ['v1.10.0', 'v1.9.0', 'v1.2.0'])

    def test_pages(self):
        pages = {1: [{'tag_name': 'v1.%d.0' % i} for i in range(rel.PER_PAGE)], 2: [{'tag_name': 'v2.0.0'}]}

        def get(url):
            return json.dumps(pages[int(url.rsplit('page=', 1)[1])]).encode()
        found = rel.list_releases('companion', get)
        self.assertEqual((len(found), found[0].tag), (rel.PER_PAGE + 1, 'v2.0.0'))

    def test_bad_answers(self):
        with self.assertRaises(rel.ReleaseError):
            rel.list_releases('2C53T', lambda u: b'{"message": "rate limit"}')
        with self.assertRaises(rel.ReleaseError):
            rel.list_releases('2C53T', lambda u: b'<html>')
        with self.assertRaises(rel.ReleaseError):
            rel.list_releases('121GW', lambda u: b'[]')

    def test_list_kept_without_get(self):
        rel.forget()
        gh = FakeGitHub()
        orig = rel._get
        rel._get = gh
        try:
            rel.list_releases('LC1020E')
            rel.list_releases('LC1020E')
            self.assertEqual(len(gh.calls), 1)
            rel.forget()
            rel.list_releases('LC1020E')
            self.assertEqual(len(gh.calls), 2)
        finally:
            rel._get = orig
            rel.forget()


def synthetic(kind, tag, names):
    v = tuple(int(x) for x in tag[1:].split('.'))
    return rel.Release(kind, v, tag, tag[1:], '', {n: 10 for n in names})


class ImageNameTest(unittest.TestCase):
    def test_lc(self):
        r = rel.list_releases('LC1020E', FakeGitHub())[0]
        self.assertEqual(rel.image_name(r), 'APP_OWN.uf2')

    def test_2c53t(self):
        r = rel.list_releases('2C53T', FakeGitHub())[0]
        self.assertEqual(rel.image_name(r), 'APP_2C53T_V1.4.17_261119.bin')

    def test_never_bootloader_files(self):
        for kind in ('LC1020E', '2C53T'):
            r = synthetic(kind, 'v1.0.0', ['STARTER.bin', 'BOOT_A.bin', 'BOOT_B.bin', 'BOOT_F.bin', 'swd_loader.bin',
                                           'APP_OWN.bin', rel.SUMS])
            with self.assertRaises(rel.ReleaseError):
                rel.image_name(r)

    def test_lc_not_the_bin(self):
        with self.assertRaises(rel.ReleaseError):
            rel.image_name(synthetic('LC1020E', 'v1.0.0', ['APP_OWN.bin', rel.SUMS]))

    def test_2c53t_pattern_strict(self):
        for bad in ('APP_2C53T.bin', 'APP_2C53T_V1.0.0.bin', 'APP_2C53T_V1.0.0_261119.bin.zip', 'APP_2C53T_V1.0_261119.bin',
                    'xAPP_2C53T_V1.0.0_261119.bin', 'APP_2C53T_V1.0.0_26119.bin'):
            with self.assertRaises(rel.ReleaseError, msg=bad):
                rel.image_name(synthetic('2C53T', 'v1.0.0', [bad, rel.SUMS]))

    def test_2c53t_version_must_match_tag(self):
        with self.assertRaises(rel.ReleaseError):
            rel.image_name(synthetic('2C53T', 'v1.4.17', ['APP_2C53T_V1.4.16_261119.bin', rel.SUMS]))

    def test_two_images_refused(self):
        with self.assertRaises(rel.ReleaseError):
            rel.image_name(synthetic('2C53T', 'v1.0.0', ['APP_2C53T_V1.0.0_261119.bin', 'APP_2C53T_V1.0.0_261120.bin', rel.SUMS]))

    def test_sums_required(self):
        with self.assertRaises(rel.ReleaseError):
            rel.image_name(synthetic('LC1020E', 'v1.0.0', ['APP_OWN.uf2']))

    def test_companion_has_none(self):
        with self.assertRaises(rel.ReleaseError):
            rel.image_name(synthetic('companion', 'v1.0.0', ['a.dmg', rel.SUMS]))


class SumsTest(unittest.TestCase):
    H1, H2 = 'a' * 64, 'B' * 64

    def test_parse(self):
        self.assertEqual(rel.parse_sums('%s  one.bin\n%s *two file.bin\n\n' % (self.H1, self.H2)),
                         {'one.bin': self.H1, 'two file.bin': self.H2.lower()})

    def test_malformed(self):
        for text in ('abc  x.bin\n', '%s x.bin\n' % ('a' * 63), '%s\n' % self.H1, 'x' * 64 + '  y\n'):
            with self.assertRaises(rel.ReleaseError, msg=text):
                rel.parse_sums(text)

    def test_conflicting_duplicate(self):
        with self.assertRaises(rel.ReleaseError):
            rel.parse_sums('%s  x\n%s  x\n' % (self.H1, self.H2))
        self.assertEqual(rel.parse_sums('%s  x\n%s  x\n' % (self.H1, self.H1)), {'x': self.H1})


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gh = FakeGitHub()
        self.lc = rel.list_releases('LC1020E', self.gh)[0]
        self.c2 = rel.list_releases('2C53T', self.gh)[0]
        self.gh.calls.clear()

    def url(self, r, name):
        return rel.ASSET_URL % (rel.REPOS[r.kind], r.tag, name)

    def test_download_and_cache(self):
        path = rel.download(self.lc, self.gh, self.tmp.name)
        self.assertEqual(path, os.path.join(self.tmp.name, 'releases', 'LC1020E', 'v1.0.0', 'APP_OWN.uf2'))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), self.gh.files[self.url(self.lc, 'APP_OWN.uf2')])
        self.assertEqual(len(self.gh.calls), 2)                              # SHA256SUMS and the image
        self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if n.endswith('.part')])
        self.gh.calls.clear()
        self.assertEqual(rel.download(self.lc, self.gh, self.tmp.name), path)
        self.assertEqual(self.gh.calls, [])                                  # from the cache, no network

    def test_2c53t(self):
        path = rel.download(self.c2, self.gh, self.tmp.name)
        self.assertTrue(path.endswith('APP_2C53T_V1.4.17_261119.bin'))
        self.assertEqual(os.path.getsize(path), 561716)
        self.assertNotIn(self.url(self.c2, 'STARTER.bin'), self.gh.calls)

    def test_tampered_cache_is_fetched_again(self):
        path = rel.download(self.lc, self.gh, self.tmp.name)
        with open(path, 'ab') as f:
            f.write(b'x')
        self.gh.calls.clear()
        rel.download(self.lc, self.gh, self.tmp.name)
        self.assertEqual(len(self.gh.calls), 2)
        self.assertEqual(os.path.getsize(path), 636928)

    def test_wrong_hash_keeps_nothing(self):
        self.gh.files[self.url(self.lc, 'APP_OWN.uf2')] = b'\x00' * 636928
        with self.assertRaisesRegex(rel.ReleaseError, 'does not match'):
            rel.download(self.lc, self.gh, self.tmp.name)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'releases')))

    def test_wrong_length(self):
        blob = b'\x01' * 100
        self.gh.files[self.url(self.lc, 'APP_OWN.uf2')] = blob
        self.gh.files[self.url(self.lc, rel.SUMS)] = ('%s  APP_OWN.uf2\n' % hashlib.sha256(blob).hexdigest()).encode()
        with self.assertRaisesRegex(rel.ReleaseError, '100 bytes'):
            rel.download(self.lc, self.gh, self.tmp.name)

    def test_name_not_in_sums(self):
        self.gh.files[self.url(self.lc, rel.SUMS)] = ('%s  other.bin\n' % ('0' * 64)).encode()
        with self.assertRaisesRegex(rel.ReleaseError, 'no hash'):
            rel.download(self.lc, self.gh, self.tmp.name)

    def test_cache_hash_file_alone_is_not_trusted(self):
        path = rel.download(self.lc, self.gh, self.tmp.name)
        with open(path, 'wb') as f:
            f.write(b'other')
        with open(path + '.sha256', 'w') as f:
            f.write('0' * 64 + '\n')
        self.gh.calls.clear()
        rel.download(self.lc, self.gh, self.tmp.name)
        self.assertEqual(len(self.gh.calls), 2)

    def test_known_hashes(self):
        known = rel.known_image_hashes('2C53T', self.gh)
        self.assertEqual(sorted(t for t, _ in known.values()), ['v1.4.16', 'v1.4.17'])
        want = hashlib.sha256(self.gh.files[self.url(self.c2, 'APP_2C53T_V1.4.17_261119.bin')]).hexdigest()
        self.assertEqual(known[want], ('v1.4.17', 'APP_2C53T_V1.4.17_261119.bin'))
        starter = hashlib.sha256(self.gh.files[self.url(self.c2, 'STARTER.bin')]).hexdigest()
        self.assertNotIn(starter, known)                                     # only the image files count
        lc = rel.known_image_hashes('LC1020E', self.gh)
        self.assertEqual(list(lc.values()), [('v1.0.0', 'APP_OWN.uf2')])

    def test_listed_hashes_cover_every_sums_line(self):
        # the slot-file gate (review_r1 finding 3): every file of the LC1020E release counts, and the
        # saved release v1.0.0 publishes no slot file
        lc = rel.listed_hashes('LC1020E', self.gh)
        self.assertEqual(sorted(lc.values()), [('v1.0.0', 'APP_OWN.bin'), ('v1.0.0', 'APP_OWN.uf2'),
                                               ('v1.0.0', 'STARTER.bin')])
        starter = hashlib.sha256(self.gh.files[self.url(self.lc, 'STARTER.bin')]).hexdigest()
        self.assertEqual(lc[starter], ('v1.0.0', 'STARTER.bin'))


class RelationTest(unittest.TestCase):
    def test_2c53t_text(self):
        r = synthetic('2C53T', 'v1.4.17', ['x'])
        self.assertEqual(rel.relation('2C53T', '1.4.16', r), 'newer')
        self.assertEqual(rel.relation('2C53T', '1.4.17', r), 'same')
        self.assertEqual(rel.relation('2C53T', '1.4.18', r), 'older')
        self.assertEqual(rel.relation('2C53T', '1.10.0', r), 'older')           # numeric, not text order
        self.assertEqual(rel.relation('2C53T', b'1.4.17\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00', r), 'same')
        for bad in ('', 'V1.4.17', '1.4', 'LC1020E V1.1 OWN', '1.4.17 beta'):
            self.assertEqual(rel.relation('2C53T', bad, r), 'unknown', bad)

    def test_meter_version(self):
        self.assertEqual(rel.meter_version('2C53T', b'1.4.17\x00\x00'), (1, 4, 17))
        self.assertIsNone(rel.meter_version('LC1020E', 'LC1020E V1.1 OWN'))
        self.assertIsNone(rel.meter_version('2C53T', 'junk'))

    def test_lc_text_carries_no_release(self):
        r = synthetic('LC1020E', 'v1.0.0', ['x'])
        self.assertEqual(rel.relation('LC1020E', 'LC1020E V1.1 OWN', r), 'unknown')
        self.assertEqual(rel.relation('LC1020E', 'LC1020E V1.1 OWN', synthetic('LC1020E', 'v1.1.0', ['x'])), 'unknown')

    def test_lc_with_own_install_record(self):
        r = synthetic('LC1020E', 'v1.1.0', ['x'])
        self.assertEqual(rel.relation('LC1020E', 'LC1020E V1.1 OWN', r, 'v1.0.0'), 'newer')
        self.assertEqual(rel.relation('LC1020E', 'LC1020E V1.1 OWN', r, 'v1.1.0'), 'same')
        self.assertEqual(rel.relation('LC1020E', 'LC1020E V1.1 OWN', r, 'v1.2.0'), 'older')
        self.assertEqual(rel.relation('LC1020E', 'LC1020E V1.1 OWN', r, 'garbage'), 'unknown')

    def test_companion(self):
        r = synthetic('companion', 'v1.2.0', ['x'])
        self.assertEqual(rel.relation('companion', '1.1.9', r), 'newer')
        self.assertEqual(rel.relation('companion', 'dev', r), 'unknown')


if __name__ == '__main__':
    unittest.main()
