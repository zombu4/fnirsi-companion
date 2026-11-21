"""The text of one release page: the section of one version of CHANGELOG.md.

    python3 -I .github/scripts/release_notes.py CHANGELOG.md VERSION URL OUT

CHANGELOG.md has one section per version, headed "## VERSION" (optionally followed by " - date" or " - unreleased").
OUT gets that section and a link to the whole changelog at URL.  Exits with 1 when the changelog has no section for
VERSION: a release is never published without its notes.
"""
import re
import sys


def section(changelog, version):
    pat = re.compile(r'^## %s(?=\s|$)' % re.escape(version), re.M)
    m = pat.search(changelog)
    if not m:
        return None
    nxt = re.compile(r'^## ', re.M).search(changelog, m.end())
    body = changelog[m.end():nxt.start() if nxt else len(changelog)]
    return body.split('\n', 1)[1].strip() if '\n' in body else ''


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__)
    path, version, url, out = argv
    with open(path, encoding='utf-8') as f:
        body = section(f.read(), version)
    if not body:
        print('release_notes: no (or an empty) section "## %s" in %s' % (version, path))
        return 1
    text = "### What's new in %s\n\n%s\n\nEarlier versions: [the whole changelog](%s).\n" % (version, body, url)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(text)
    print('release_notes: %s, %d bytes' % (out, len(text.encode('utf-8'))))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
