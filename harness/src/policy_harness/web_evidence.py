"""Static page structure and bounded model views; no requests or script execution."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


def decode_text(raw, content_type, *, partial=False):
    import codecs
    import re
    if not any(kind in content_type.lower() for kind in (
            'text/', 'application/json', 'application/xml', 'application/xhtml+xml',
            'application/javascript', 'application/ecmascript')):
        raise ValueError('This content format needs a controlled extraction capability.')
    charset = re.search(r'charset=[\"\']?([\w-]+)', content_type, re.IGNORECASE)
    try:
        encoding = charset.group(1) if charset else 'utf-8'
        if partial:
            # A known byte limit may split a character. Keep all complete
            # characters and retain the exact original bytes separately.
            return codecs.getincrementaldecoder(encoding)(errors='strict').decode(raw, final=False)
        return raw.decode(encoding)
    except (UnicodeError, LookupError):
        raise ValueError('The page encoding needs explicit handling; text was not silently replaced.') from None


class PageStructure(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = self.base = url
        self.links, self.forms, self.scripts, self.metadata = [], [], [], []
        self.anchor = self.form = None
        self.loc = False
        self.inline_scripts = 0
        self.base_seen = False

    def address(self, value):
        try:
            target = urljoin(self.base, value or '')
            return target if urlsplit(target).scheme in {'http', 'https'} else None
        except ValueError:
            return None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'base' and not self.base_seen and a.get('href'):
            self.base = self.address(a['href']) or self.base
            self.base_seen = True
        elif tag in {'a', 'area', 'link'} and a.get('href'):
            url = self.address(a['href'])
            if url:
                item = {'url': url, 'text': a.get('title') or a.get('aria-label') or '',
                        'rel': a.get('rel', '')}
                self.links.append(item)
                if tag == 'a':
                    self.anchor = item
        elif tag == 'script':
            if a.get('src'):
                url = self.address(a['src'])
                if url:
                    self.scripts.append({'url': url, 'type': a.get('type', '')})
            else:
                self.inline_scripts += 1
        elif tag == 'form':
            self.form = {'action': self.address(a.get('action')),
                         'method': (a.get('method') or 'get').upper(), 'fields': []}
            self.forms.append(self.form)
        elif tag in {'input', 'textarea', 'select', 'button'} and self.form is not None:
            # Values, hidden tokens and user data are not needed to describe a form.
            self.form['fields'].append({'tag': tag, **{k: a[k] for k in
                ('name', 'type', 'placeholder', 'aria-label', 'autocomplete') if a.get(k)},
                'required': 'required' in a})
        elif tag == 'meta':
            name = a.get('name') or a.get('property')
            if name and a.get('content'):
                self.metadata.append({'name': name, 'content': a['content']})
        elif tag == 'loc':
            self.loc = True

    def handle_endtag(self, tag):
        if tag == 'a': self.anchor = None
        if tag == 'form': self.form = None
        if tag == 'loc': self.loc = False

    def handle_data(self, text):
        if self.anchor is not None and text.strip():
            self.anchor['text'] = (self.anchor['text'] + ' ' + text.strip()).strip()
        if self.loc and text.strip():
            url = self.address(text.strip())
            if url:
                self.links.append({'url': url, 'text': '', 'rel': 'sitemap'})

    def result(self):
        links, seen = [], set()
        for item in self.links:
            key = (item['url'], item['text'], item['rel'])
            if key not in seen:
                seen.add(key); links.append(item)
        return {'links': links, 'forms': self.forms, 'scripts': self.scripts,
                'metadata': self.metadata, 'inline_script_count': self.inline_scripts,
                'rendering': 'Static response only. JavaScript was not executed; runtime requests and submitted form behavior are unobserved.'}


def document_structure(text, url, content_type):
    if 'html' not in content_type.lower() and 'xml' not in content_type.lower():
        return None
    parser = PageStructure(url)
    parser.feed(text)
    return parser.result()


def source_overview(source, index, *, budget=2200):
    """Give each source its own space so a large first page cannot hide later pages."""
    from .store import canonical
    text = source.get('text', '')
    result = {k: source[k] for k in ('url', 'requested_url', 'title', 'retrieved_at', 'content_type',
                                    'truncated', 'body_complete', 'received_bytes', 'response_limit_bytes',
                                    'coverage_note') if k in source}
    result.update(source=index, total_text_chars=len(text), text=text[:budget // 2])
    document = source.get('document')
    if document:
        result['document'] = {'rendering': document['rendering'],
                              'inline_script_count': document.get('inline_script_count', 0)}
        left = budget - len(result['text'])
        # Each kind gets space; long link lists cannot conceal forms and scripts.
        for key in ('forms', 'scripts', 'metadata', 'links'):
            values = document.get(key, [])
            if key == 'links':
                # Navigation labels are useful before stylesheets and alternate
                # language metadata. Preserve the original order within each set.
                values = sorted(values, key=lambda item: not bool(item.get('text')))
            used, preview = 0, []
            for value in values:
                size = len(canonical(value))
                if used + size > max(200, left // 4):
                    break
                used += size; preview.append(value)
            result['document'][key] = preview
            result['document'][key + '_total'] = len(values)
    result['excerpt_only'] = True
    result['read_more'] = ('history_read(start=operation index, field=data.sources.' + str(index) +
        '.text or .document, query=phrase); view=web_response, source=' + str(index) +
        ' reads saved raw HTML/JS with query/offset. It makes no new request.')
    return result
