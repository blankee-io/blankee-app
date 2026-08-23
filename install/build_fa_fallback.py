#!/usr/bin/env python3
"""
Generate the Font Awesome Pro -> Free fallback stylesheet, and audit for gaps.

    python3 install/build_fa_fallback.py            # regenerate the stylesheet
    python3 install/build_fa_fallback.py --check     # audit only, exit 1 on a gap

WHY A GENERATOR RATHER THAN A HAND-WRITTEN CSS FILE

Font Awesome 7 keeps each glyph in a custom property - `.fa-user{--fa:"\\f007"}` -
and applies the machinery that renders it (family, weight, the `:before` that
consumes `--fa`) only to a fixed list of class names: .fa, .fa-solid,
.fa-regular, .fa-brands and their short forms. Pro's extra classes - .fa-light,
.fa-thin, .fa-duotone, .fa-sharp, .fa-kit - are absent from Free's stylesheet
entirely, so an element carrying one of them gets no font and no content at all.

Setting `--fa` on those classes therefore is not enough. They need the same base
declarations Free applies to .fa-solid. Those declarations are Font Awesome's
internals and change between releases, so this script lifts them out of the
bundled Free CSS instead of hard-coding a copy that would rot.

The audit half is the reason this is worth a script at all: it reports any Pro
icon used in the app with no entry in the map, which is what stops a future Pro
icon from quietly becoming a blank box for anyone without a Pro licence.
"""

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FREE_CSS = os.path.join(ROOT, 'static', 'fontawesome-free', 'css', 'all.min.css')
MAP_FILE = os.path.join(ROOT, 'install', 'fa_fallback_map.json')
OUT_CSS = os.path.join(ROOT, 'static', 'css', 'fa-pro-fallback.css')
SCAN_DIRS = ('templates', os.path.join('static', 'css'), os.path.join('static', 'js'))

WEIGHTS = {'solid': 900, 'regular': 400}

# Utility and animation classes share the fa- prefix but are not icons.
NOT_ICONS = {
    'fa', 'fa-solid', 'fa-regular', 'fa-brands', 'fa-classic', 'fa-light', 'fa-thin',
    'fa-duotone', 'fa-sharp', 'fa-kit', 'fas', 'far', 'fab', 'fal', 'fat', 'fad', 'fak',
    'fa-fw', 'fa-lg', 'fa-sm', 'fa-xs', 'fa-xl', 'fa-2xl', 'fa-1x', 'fa-2x', 'fa-3x',
    'fa-4x', 'fa-5x', 'fa-6x', 'fa-7x', 'fa-8x', 'fa-9x', 'fa-10x', 'fa-spin',
    'fa-spin-pulse', 'fa-spin-reverse', 'fa-pulse', 'fa-beat', 'fa-beat-fade', 'fa-fade',
    'fa-bounce', 'fa-shake', 'fa-flip', 'fa-flip-horizontal', 'fa-flip-vertical',
    'fa-flip-both', 'fa-rotate-90', 'fa-rotate-180', 'fa-rotate-270', 'fa-rotate-by',
    'fa-border', 'fa-pull-left', 'fa-pull-right', 'fa-stack', 'fa-stack-1x',
    'fa-stack-2x', 'fa-inverse', 'fa-li', 'fa-ul', 'fa-layers',
}


def load_map():
    with open(MAP_FILE, encoding='utf-8') as f:
        raw = json.load(f)
    return {
        'family': raw['free_family'],
        'styles': {k: v for k, v in raw['styles'].items() if k != '_about'},
        'icons': {k: v for k, v in raw['icons'].items() if k != '_about'},
        'ignore': set(raw.get('ignore', {}).get('entries', [])),
    }


def free_css():
    if not os.path.exists(FREE_CSS):
        sys.exit(f'Font Awesome Free is missing: {FREE_CSS}\n'
                 f'It should be committed to the repository - see THIRD-PARTY-NOTICES.md.')
    with open(FREE_CSS, encoding='utf-8') as f:
        return f.read()


def free_glyphs(css):
    """Every icon name Free defines, mapped to its `--fa` value."""
    glyphs = {}
    for m in re.finditer(r'((?:\.fa-[a-z0-9-]+,?)+)\{--fa:([^;}]+)', css):
        value = m.group(2).strip()
        for name in re.findall(r'fa-[a-z0-9-]+', m.group(1)):
            glyphs[name] = value
    return glyphs


def base_declarations(css):
    """
    The declaration block Free applies to .fa-solid, lifted verbatim.

    This is what makes an element an icon at all - font family, the weight
    default, display, width, smoothing. Pro's own classes need it because Free's
    stylesheet has never heard of them.
    """
    m = re.search(r'\.fa,[^{]*\.fa-solid[^{]*\{([^}]*)\}', css)
    if not m:
        sys.exit('Could not find the .fa base rule in the bundled Free CSS. '
                 'Font Awesome may have restructured it; this script needs updating.')
    return m.group(1).strip()


def scan_used():
    """Every fa- class used in the app, mapped to the files it appears in."""
    used = {}
    for rel in SCAN_DIRS:
        base = os.path.join(ROOT, rel)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ('fontawesome', 'fontawesome-free')]
            for name in filenames:
                # ._* are macOS AppleDouble resource forks: binary, and ignored by git
                if name.startswith('._') or not name.endswith(('.html', '.css', '.js')):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, encoding='utf-8', errors='ignore') as f:
                        body = f.read()
                except OSError:
                    continue
                short = os.path.relpath(path, ROOT).replace('\\', '/')
                for attr in re.findall(r'class\s*=\s*"([^"]*)"', body):
                    for token in attr.split():
                        if token.startswith('fa-') and token not in NOT_ICONS:
                            used.setdefault(token, set()).add(short)
    return used


def audit(cfg, glyphs, used):
    """Pro-only classes in use with no mapping. Returns a list of problems."""
    problems = []
    for name in sorted(used):
        if name in glyphs or name in cfg['icons'] or name in cfg['ignore']:
            continue
        where = ', '.join(sorted(used[name])[:3])
        problems.append(f'{name}  (used in {where})')

    # A mapping that points at an icon Free does not have would render nothing.
    for pro, target in sorted(cfg['icons'].items()):
        if target['icon'] not in glyphs:
            problems.append(f'{pro} -> {target["icon"]} is not in Font Awesome Free')
        if target['style'] not in WEIGHTS:
            problems.append(f'{pro} has unknown style {target["style"]!r}')
    return problems


def generate(cfg, css, glyphs):
    family = cfg['family']
    base = base_declarations(css)
    pro_styles = sorted(cfg['styles'])
    selectors = ', '.join('.' + s for s in pro_styles)

    out = [
        '/*',
        ' * Font Awesome Pro -> Free fallback.',
        ' *',
        ' * GENERATED by install/build_fa_fallback.py - do not edit by hand.',
        ' * To change an icon, edit install/fa_fallback_map.json and re-run it.',
        ' *',
        ' * Loaded only when static/fontawesome/ (the Pro build) is absent - see',
        ' * templates/_fontawesome.html. Must load AFTER Free\'s all.min.css: both',
        ' * define --fa at the same specificity, so source order decides.',
        ' */',
        '',
        '/* Used by the two rules in style.css that name a Font Awesome family',
        '   directly rather than going through an icon class. */',
        ':root {',
        f'    --blankee-fa-family: "{family}";',
        '}',
        '',
        '/* Pro style classes do not exist in Free, so they inherit none of the',
        '   machinery that turns an element into an icon. These are Free\'s own',
        '   declarations for .fa-solid, lifted from the bundled stylesheet. */',
        f'{selectors} {{',
    ]
    for decl in base.split(';'):
        decl = decl.strip()
        if decl:
            out.append(f'    {decl};')
    out.append('}')
    out.append('')
    out.append('/* ...and the rule that actually emits the glyph. Free scopes this to its')
    out.append('   own class names, so without it a Pro class renders an empty box. */')
    out.append(f'{", ".join(s + "::before" for s in ("." + p for p in pro_styles))} {{')
    out.append('    content: var(--fa);')
    out.append('}')
    out.append('')

    non_default = {s: w for s, w in cfg['styles'].items() if w != 900}
    if non_default:
        out.append('/* Style classes whose default weight is not solid. */')
        for style, weight in sorted(non_default.items()):
            out.append(f'.{style} {{ --fa-style: {weight}; }}')
        out.append('')

    out.append('/* Pro-only icon names and Kit icons, each borrowing a Free glyph.')
    out.append('   --fa-style is set per icon, not per style class, because two Kit')
    out.append('   icons can share a class and still want different weights. */')

    solid, regular = [], []
    for pro, target in sorted(cfg['icons'].items()):
        weight = WEIGHTS[target['style']]
        line = (f'.{pro} {{ --fa: {glyphs[target["icon"]]}; --fa-style: {weight}; }}'
                f'  /* {target["icon"]} */')
        (regular if weight == 400 else solid).append(line)

    out.append('')
    out.append('/* solid */')
    out.extend(solid)
    if regular:
        out.append('')
        out.append('/* regular - the only icons drawn from fa-regular-400.woff2 */')
        out.extend(regular)
    out.append('')

    text = '\n'.join(out)
    os.makedirs(os.path.dirname(OUT_CSS), exist_ok=True)
    with open(OUT_CSS, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return text, len(solid) + len(regular), len(pro_styles)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true',
                        help='audit only; exit 1 if a Pro icon has no fallback')
    args = parser.parse_args()

    cfg = load_map()
    css = free_css()
    glyphs = free_glyphs(css)
    used = scan_used()

    print(f'Font Awesome Free:  {len(glyphs)} icons  ({os.path.relpath(FREE_CSS, ROOT)})')
    print(f'app uses:           {len(used)} distinct icon classes')
    print(f'mapped:             {len(cfg["icons"])} Pro icons, {len(cfg["styles"])} style classes')

    problems = audit(cfg, glyphs, used)
    if problems:
        print(f'\n{len(problems)} problem(s):')
        for p in problems:
            print(f'  - {p}')
        print('\nAdd each to install/fa_fallback_map.json, or to its "ignore" list if it')
        print('is not a real icon name.')
        return 1

    print('\naudit: every Pro icon in use has a Free fallback')

    if args.check:
        return 0

    text, n_icons, n_styles = generate(cfg, css, glyphs)
    print(f'wrote {os.path.relpath(OUT_CSS, ROOT)}: '
          f'{len(text.splitlines())} lines, {n_icons} icons, {n_styles} style classes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
