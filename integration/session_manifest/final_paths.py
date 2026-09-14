"""Lexical candidates from final replies; filesystem policy stays in manifest."""
import re
from urllib.parse import unquote, urlsplit


_LINK = re.compile(r'!?\[[^\]\n]*\]\((<[^>\n]+>|[^\n]*?)\)')
_CODE = re.compile(r'(?<!`)(`{1,2})(?!`)([^\n]*?)(?<!`)\1(?!`)')
_QUOTED = re.compile(r'''(["'])([^\n"']+)\1''')
_REFERENCE = re.compile(r'!?\[([^\]\n]+)\]\[([^\]\n]*)\]')
_DEFINITION = re.compile(r'^\s*\[([^\]\n]+)\]:\s*(<[^>\n]+>|\S+).*$', re.M)
_SHORTCUT = re.compile(r'!?\[([^\]\n]+)\]')
_URL = re.compile(r'(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)[^\s<>`]+')
_STRONG = re.compile(r'(?<!\*)\*\*([^\n]+?)\*\*(?!\*)')
_FILE_SUFFIX = re.compile(r'\.[A-Za-z0-9]+$')
_EXPLICIT = re.compile(r'(?<![\w/])(?:~/|/|\./|[\w.-]+/)[^\s`\"\'<>|，；。]+')
_FILE = re.compile(r'(?<![\w/._-])([\w·/._()（）-]+\.[A-Za-z0-9]+)(?![\w/_-]|\.[\w])')
_MEDIA_BRACKETED = re.compile(r'MEDIA:<([^>\r\n]+)>')
_MEDIA_LINE = re.compile(r'^[ \t]*MEDIA:[ \t]*(?!<)([^\r\n]*?\S)[ \t]*\r?$', re.M)
_MEDIA_LEGACY = re.compile(r'MEDIA:([^\s\]]+)')


def media_references(text: str) -> list[str]:
    """Return local/remote MEDIA refs without splitting standalone paths on spaces."""
    if not isinstance(text, str) or not text:
        return []
    result: list[str] = []

    def collect(match):
        value = (match.group(1) or '').strip()
        if value:
            result.append(value)
        return ' ' * len(match.group(0))

    # Brackets are an explicit boundary and may be used inline. A legacy
    # unbracketed ref may contain spaces only when MEDIA owns the whole line;
    # inline legacy refs retain their historical whitespace boundary.
    remaining = _MEDIA_BRACKETED.sub(collect, text)
    remaining = _MEDIA_LINE.sub(collect, remaining)
    for match in _MEDIA_LEGACY.finditer(remaining):
        value = (match.group(1) or '').strip()
        if value:
            result.append(value)
    return list(dict.fromkeys(result))


def candidates(text: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    result = []

    def add(value):
        value = value.strip()
        try:
            remote = bool(urlsplit(value).scheme)
        except ValueError:
            return
        if value and not remote and not value.startswith('//'):
            result.append(value)

    def link(match):
        target = match.group(1).strip()
        if target.startswith('<') and target.endswith('>'):
            target = target[1:-1]
        else:
            target = re.sub(r'\s+[\"\'][^\n]*[\"\']$', '', target)
        add(unquote(target))
        return ' ' * len(match.group(0))

    # Mask the entire link, including its label: a remote filename is never
    # permission to select an unrelated local file with the same basename.
    definitions = {m.group(1).casefold(): m.group(2).strip('<>') for m in _DEFINITION.finditer(text)}
    remaining = _DEFINITION.sub(lambda m: ' ' * len(m.group(0)), text)

    def reference(match):
        target = definitions.get((match.group(2) or match.group(1)).casefold())
        if target:
            add(unquote(target))
        return ' ' * len(match.group(0))

    remaining = _REFERENCE.sub(reference, remaining)
    remaining = _LINK.sub(link, remaining)

    def shortcut(match):
        target = definitions.get(match.group(1).casefold())
        if target is None:
            return match.group(0)
        add(unquote(target))
        return ' ' * len(match.group(0))

    remaining = _SHORTCUT.sub(shortcut, remaining)
    remaining = _URL.sub(lambda m: ' ' * len(m.group(0)), remaining)

    def code(match):
        value = match.group(2).strip()
        # Inline code containing a statement is scanned as prose/code below;
        # a delimited path may contain spaces or have no extension.
        if not any(char in value for char in '=\"\'{};') and '(' not in value:
            add(value)
            return ' ' * len(match.group(0))
        return match.group(0)

    remaining = _CODE.sub(code, remaining)

    def strong(match):
        value = match.group(1).strip()
        # Markdown emphasis supplies a safe boundary for filenames containing
        # spaces. Do not promote arbitrary emphasized prose or headings.
        if _FILE_SUFFIX.search(value):
            add(value)
        return ' ' * len(match.group(0))

    remaining = _STRONG.sub(strong, remaining)
    remaining = _QUOTED.sub(lambda m: (add(m.group(2)) or ' ' * len(m.group(0))), remaining)
    for match in _EXPLICIT.finditer(remaining):
        add(match.group(0).rstrip('),;:!?。，；：'))
    for match in _FILE.finditer(remaining):
        add(match.group(1))
    return list(dict.fromkeys(result))
