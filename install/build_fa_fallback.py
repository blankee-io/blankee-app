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
REGULAR_FILE = os.path.join(ROOT, 'install', 'fa_free_regular.txt')
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
        'weight_overrides': {k: v for k, v in raw.get('weight_overrides', {}).items()
                             if k != '_about'},
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


def free_regular():
    """
    The icon names Free ships in regular weight.

    Free has ~2,500 icons but only ~270 in regular. Whether a name exists in
    Free is therefore not the same question as whether it renders in the style
    the markup asked for: `fa-regular fa-lock` is an empty box on a Free install,
    because the regular font has no lock glyph, even though fa-lock is present.

    That distinction is the whole reason this file exists. Missing it left eight
    icons blank - the settings, admin and log-out entries in the profile menu
    among them - while the audit reported no problem at all.
    """
    if not os.path.exists(REGULAR_FILE):
        sys.exit(f'Missing {os.path.relpath(REGULAR_FILE, ROOT)}, which lists the icons '
                 f'Font Awesome Free ships in regular weight. Without it, icons used '
                 f'with fa-regular cannot be checked.')
    names = set()
    with open(REGULAR_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                names.add(line)
    return names


def scan_regular_usage():
    """Icons used with fa-regular / far, mapped to the files using them."""
    found = {}
    for rel in SCAN_DIRS:
        base = os.path.join(ROOT, rel)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in ('fontawesome', 'fontawesome-free')]
            for name in filenames:
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
                    tokens = attr.split()
                    if 'fa-regular' not in tokens and 'far' not in tokens:
                        continue
                    for token in tokens:
                        if token.startswith('fa-') and token not in NOT_ICONS:
                            found.setdefault(token, set()).add(short)
    return found


def needs_solid(cfg, regular_family, regular_usage):
    """
    Icons used with fa-regular whose glyph only exists in Free's solid font.

    The effective target matters, not the class name: a Pro icon mapped to a
    Free one is checked against the icon it actually borrows from. A mapping
    that deliberately asks for regular is left alone.
    """
    out = {}
    for icon, files in regular_usage.items():
        entry = cfg['icons'].get(icon)
        if entry and entry['style'] == 'regular':
            continue                      # deliberately regular, and verified elsewhere
        target = entry['icon'] if entry else icon
        if target[3:] not in regular_family:
            out[icon] = (target, files)
    return out


APP_CSS = os.path.join(ROOT, 'static', 'css', 'style.css')

# Selectors that plausibly target a Font Awesome element.
ICON_SELECTOR = re.compile(r"""(^|[\s>+~,])i(\b|[.:\[])|\.fa[-s]|\.far|\bfa-|\[class\*?=['"]?fa""")


def scan_weight_overrides():
    """Rules in the app's own CSS that put a literal font-weight on an icon.

    These defeat everything else in this script. Free's base rule reads
    `font-weight: var(--fa-style, 900)`, so setting --fa-style is how an
    icon's weight gets chosen. A plain `font-weight: 300` in style.css
    overrides that outright - and because Free ships only 400 and 900 faces,
    300 resolves to regular, so every solid-only glyph under that selector is
    an empty box no matter what this script emitted for it.

    `.dropdown-link i { font-weight: 300 }` did precisely that to the whole
    profile menu: correct under Pro, which has a 300 face, and silently blank
    under Free.

    Returns {selector: (line_number, literal_value)}.
    """
    try:
        with open(APP_CSS, encoding='utf-8', errors='replace') as f:
            css = f.read()
    except OSError:
        return {}

    # Blank out comments while keeping line numbering intact.
    css = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), css, flags=re.S)

    found = {}
    for m in re.finditer(r'([^{}]+)\{([^}]*)\}', css):
        sel, body = m.group(1).strip(), m.group(2)
        decl = re.search(r'(?<!-)font-weight\s*:\s*([^;!]+)', body)
        if not decl:
            continue
        value = decl.group(1).strip()
        # A var() already defers to the icon's own weight, which is the fix.
        if 'var(' in value or not ICON_SELECTOR.search(sel):
            continue
        found.setdefault(' '.join(sel.split()), (css[:m.start()].count('\n') + 1, value))
    return found


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


def audit(cfg, glyphs, used, weight_overrides=None):
    """Pro-only classes in use with no mapping. Returns a list of problems."""
    problems = []

    # A literal font-weight on an icon selector overrides --fa-style, so it
    # blanks solid-only glyphs under Free however well they are mapped. Each
    # one has to be acknowledged in the map.
    handled = set(cfg.get('weight_overrides', {}))
    for sel, (line, value) in sorted((weight_overrides or {}).items()):
        if sel not in handled:
            problems.append(
                f'style.css:{line}  `{sel}` sets font-weight: {value} on an icon. '
                f'Under Free that forces the regular face and blanks any solid-only '
                f'glyph it covers. Add it to "weight_overrides" in the map, or write '
                f'font-weight: var(--fa-style, {value}) instead.')
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


def generate(cfg, css, glyphs, downgrades):
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

    if downgrades:
        out.append('/* Icons the markup asks for in regular that Free only ships in solid.')
        out.append('   Free has ~2,500 icons but only ~270 in regular, so `fa-regular')
        out.append('   fa-lock` is an empty box: the name exists, the glyph does not. These')
        out.append('   render solid instead - heavier than intended, and visible, which is')
        out.append('   the better of the two.')
        out.append('')
        out.append('   Scoped to .fa-regular/.far so the same icon used with fa-solid')
        out.append('   elsewhere is untouched. */')
        for icon in sorted(downgrades):
            target, _ = downgrades[icon]
            note = f'  /* {target} is solid-only */' if target == icon else \
                   f'  /* via {target}, solid-only */'
            out.append(f'.fa-regular.{icon}, .far.{icon} {{ --fa-style: 900; }}{note}')
        out.append('')

    overrides = [k for k in cfg.get('weight_overrides', {}) if not k.startswith('_')]
    if overrides:
        out.append("/* The app's own CSS sets a literal font-weight on these, which")
        out.append('   overrides --fa-style and, with only a 400 and a 900 face in Free,')
        out.append('   collapses every solid-only glyph under them into an empty box.')
        out.append('   Handing the weight back to the icon costs nothing here, because')
        out.append('   this file loads only when Pro is absent, so a Pro install keeps')
        out.append('   its own look. */')
        for sel in sorted(overrides):
            note = cfg['weight_overrides'][sel]
            suffix = f'  /* {note} */' if isinstance(note, str) else ''
            out.append(f'{sel} {{ font-weight: var(--fa-style, 900); }}' + suffix)
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
    regular_family = free_regular()
    downgrades = needs_solid(cfg, regular_family, scan_regular_usage())
    weight_overrides = scan_weight_overrides()

    print(f'Font Awesome Free:  {len(glyphs)} icons, {len(regular_family)} of them in regular')
    print(f'app uses:           {len(used)} distinct icon classes')
    print(f'mapped:             {len(cfg["icons"])} Pro icons, {len(cfg["styles"])} style classes')
    if downgrades:
        print(f'forced to solid:    {len(downgrades)} used with fa-regular but solid-only in Free')
        for icon in sorted(downgrades):
            target, files = downgrades[icon]
            via = '' if target == icon else f' (via {target})'
            print(f'                      {icon}{via} - {", ".join(sorted(files)[:2])}')

    if weight_overrides:
        listed = set(cfg.get('weight_overrides', {}))
        n_ok = sum(1 for sel in weight_overrides if sel in listed)
        print(f'weight overrides:   {len(weight_overrides)} in style.css ({n_ok} handled)')
        for sel, (line, value) in sorted(weight_overrides.items()):
            flag = '' if sel in listed else '  <- UNHANDLED'
            print(f'                      {sel} -> {value} (style.css:{line}){flag}')

    problems = audit(cfg, glyphs, used, weight_overrides)
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

    text, n_icons, n_styles = generate(cfg, css, glyphs, downgrades)
    print(f'wrote {os.path.relpath(OUT_CSS, ROOT)}: '
          f'{len(text.splitlines())} lines, {n_icons} icons, {n_styles} style classes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
