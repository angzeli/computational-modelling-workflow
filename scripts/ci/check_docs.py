"""Offline checks for tracked Markdown fences and local Markdown/HTML links."""
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]


def local_link_error(source, target, root):
    target = target.strip()
    if target.startswith('<'):
        target = target[1:target.index('>')] if '>' in target else target
    else:
        target = re.split(r'\s+["\']', target, maxsplit=1)[0]
    url = urlsplit(target)
    if url.scheme or url.netloc or not url.path or target.startswith('/'):
        return None  # External/site-root routes and anchors are not filesystem links.
    path = (source.parent / unquote(url.path)).resolve()
    if not path.is_relative_to(root.resolve()):
        return None  # References to separate checkouts are not checked/read here.
    return None if path.exists() else f'{source.relative_to(root)}: missing local link {target}'


def markdown_errors(path, root):
    errors, fence = [], None
    for number, line in enumerate(path.read_text().splitlines(), 1):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if marker:
            token, suffix = marker.groups()
            if fence is None:
                fence = (token, number)
            elif token[0] == fence[0][0] and len(token) >= len(fence[0]) and not suffix.strip():
                fence = None
            continue
        if fence:
            continue
        # Ignore inline code and illustrative placeholder links.
        text = re.sub(r'`[^`]*`', '', line)
        for target in re.findall(r'\]\(([^)\n]+)\)', text):
            if not any(c in target for c in ('{', '}', '*')):
                if error := local_link_error(path, target, root):
                    errors.append(f'{number}: {error}')
    if fence:
        errors.append(f'{path.relative_to(root)}:{fence[1]}: unclosed code fence')
    return errors


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        self.targets.extend(value for key, value in attrs if key in {'href', 'src'} and value)


def main():
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    errors, count = [], 0
    for name in names:
        path = ROOT / name
        if path.is_symlink():
            errors.append(f'{name}: tracked symlink is not read')
            continue
        if path.suffix == '.md' and path.is_file():
            count += 1
            errors.extend(markdown_errors(path, ROOT))
        elif name.startswith('docs/website/') and path.suffix == '.html' and path.is_file():
            count += 1
            links = Links(); links.feed(path.read_text())
            errors.extend(error for target in links.targets
                          if (error := local_link_error(path, target, ROOT)))
    print(f'Documentation checks: {count} tracked Markdown/HTML files, {len(errors)} errors')
    print('\n'.join(errors))
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
