"""Pack the verified local release into a separate set of Project Sources."""
from io import BytesIO
from itertools import accumulate
from pathlib import Path

from pypdf import PdfWriter

from .store import digest

MAX_SOURCES = 4
ASSESSMENT_PREFIXES = ('assignments-', 'practice-tests-', 'solutions-')


def pdf_parts(stem, entries, max_bytes):
    if not entries:
        return []
    pending = [entries]
    parts = []
    while pending:
        group = pending.pop(0)
        stream = BytesIO()
        offsets = []
        with PdfWriter() as writer:
            for name, data in group:
                start = len(writer.pages) + 1
                writer.append(BytesIO(data), import_outline=False)
                offsets.append((name, start))
            writer.write(stream)
        if stream.tell() <= max_bytes:
            parts.append((stream.getvalue(), offsets))
        elif len(group) == 1:
            raise ValueError(f'{group[0][0]} exceeds the compact export size limit.')
        else:
            sizes = list(accumulate(len(data) for _, data in group))
            middle = min(range(1, len(group)), key=lambda i: abs(2 * sizes[i - 1] - sizes[-1]))
            pending[0:0] = [group[:middle], group[middle:]]
            if len(parts) + len(pending) >= MAX_SOURCES:
                raise ValueError('The compact export needs more than four Sources.')
    return [(f'{stem}.pdf' if len(parts) == 1 else f'{stem}-{i:02}.pdf', data, offsets)
            for i, (data, offsets) in enumerate(parts, 1)]


def compact_sources(folder, manifest, max_bytes):
    """Return named bytes; exceed the budget explicitly instead of dropping content."""
    lectures, assessments, markdown = [], [], []
    for entry in manifest['files']:
        name = entry['name']
        if Path(name).name != name:
            raise ValueError('Invalid bundle name')
        source = (folder / name).resolve()
        if source.parent != folder:
            raise ValueError('Bundle file is outside its release folder.')
        data = source.read_bytes()
        if digest(data) != entry['hash']:
            raise ValueError('Bundle hash mismatch; rebuild before uploading')
        if name.endswith('.pdf'):
            group = assessments if name.startswith(ASSESSMENT_PREFIXES) else lectures
            group.append((name, data))
        elif name.endswith('.md'):
            if name == 'course-index.md':
                release_line = f'\n\nBundle release: {manifest["release"]}\n\n'.encode()
                data = data.replace(release_line, b'\n\n', 1)
            markdown.append(f'## {name}\n\n'.encode() + data)
        else:
            raise ValueError(f'Unsupported compact export input: {name}')
    pdfs = (pdf_parts('lectures-and-info', lectures, max_bytes)
            + pdf_parts('assignments-and-tests', assessments, max_bytes))
    page_index = ['# Compact PDF page cross-reference',
                  'Use these offsets with the original page references in the course index.']
    for name, _, offsets in pdfs:
        for original, start in offsets:
            page_index.append(f'- {original} starts at page {start} of {name}.')
    blocks = ['\n\n'.join(page_index).encode(), *markdown]
    separator = b'\n\n---\n\n'
    parts = []
    current = bytearray()
    for block in blocks:
        if len(block) > max_bytes:
            raise ValueError('A Markdown section exceeds the compact export size limit.')
        if current and len(current) + len(separator) + len(block) > max_bytes:
            parts.append(bytes(current))
            current.clear()
        if current:
            current.extend(separator)
        current.extend(block)
    parts.append(bytes(current))
    if len(parts) + len(pdfs) > MAX_SOURCES:
        raise ValueError('The compact export needs more than four Sources.')
    return [(f'master.md' if i == 1 else f'master-{i:02}.md', data)
            for i, data in enumerate(parts, 1)] + [(name, data) for name, data, _ in pdfs]
