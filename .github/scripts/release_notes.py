#!/usr/bin/env python3
"""
Print the CHANGELOG sections one release covers, for `gh release create --notes-file`.

Not simply "the section matching this tag". Public main is a squash of private
main, so a single published release can carry several private versions: 1.2.0
shipped 1.1.5 inside it, because public went straight from 1.1.3 to 1.2.0 and
1.1.5 never had a commit of its own out there. So this prints every section
above the previous release, which is what that release actually contains.

    release_notes.py CHANGELOG.md v1.3.0 [v1.2.2]

Exits non-zero when the tag has no section of its own. That is not a corner
case to paper over - it means a release went out with nothing written down, and
a red mark on the tag push is the cheapest place to find that out.
"""
import re
import sys

# The file writes "## 1.3.0 - 2026-08-26" with an em dash. Match the version and
# stop, rather than trying to agree with the punctuation.
HEADING = re.compile(r'^##\s+(\d+\.\d+\.\d+(?:-rc\.\d+)?)\b')


def parts(version):
    """1.2.10 sorts after 1.2.9, which a string compare gets backwards."""
    core = version.split('-')[0]
    return tuple(int(n) for n in core.split('.'))


def sections(text):
    """[(version, body)] in file order, newest first as the file is written."""
    found, current = [], None
    for line in text.splitlines(keepends=True):
        m = HEADING.match(line)
        if m:
            current = [m.group(1), line]
            found.append(current)
        elif current is not None:
            current[1] += line
    return [(v, b) for v, b in found]


def main(argv):
    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path, tag = argv[1], argv[2].lstrip('v')
    prev = argv[3].lstrip('v') if len(argv) > 3 and argv[3] else None

    with open(path, encoding='utf-8') as f:
        found = sections(f.read())

    if not any(v == tag for v, _ in found):
        print(f'{path} has no "## {tag}" section', file=sys.stderr)
        return 1

    top = parts(tag)
    floor = parts(prev) if prev else None
    wanted = [b for v, b in found
              if parts(v) <= top and (floor is None or parts(v) > floor)]

    sys.stdout.write(''.join(wanted).strip() + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
