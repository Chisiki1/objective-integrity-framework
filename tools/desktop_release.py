"""Add the desktop and showcase to an exact committed beta release.

The input is a stopped, isolated Windows build. User data, caches and generated
console launchers are never distributed. Output must be a new directory.
"""
from pathlib import Path
import argparse
import json
import stat
import zipfile
import release
import plugin

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {'.runtime', '.work', '.build-python', '.venv', '__pycache__', '.pytest_cache', '.git'}


def files(root):
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if set(relative.parts) & EXCLUDE or path.suffix == '.pyc' or relative.parts[:2] == ('python', 'Scripts'):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Redirected build member: ' + relative.as_posix())
        if path.is_file():
            yield relative.as_posix(), path


def archive(path, prefix, members):
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, raw in sorted(members.items()):
            entry = zipfile.ZipInfo(prefix + '/' + name, date_time=(2026, 9, 28, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            z.writestr(entry, raw)


def build(base, desktop, destination):
    identity, _ = release.source_bundle(ROOT)
    version = identity['version']
    if '-beta.' not in version:
        raise ValueError('This profile is for a desktop beta release')
    manifest = json.loads((base/'release.json').read_bytes())
    if any(manifest.get(k) != identity[k] for k in ('source_commit','source_tree','version')):
        raise ValueError('Core assets have a different source identity')
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('Use an absent destination with an existing parent')
    for parent in (ROOT, base, desktop):
        release.no_links(parent)
        if release.within(destination, parent) or release.within(parent, destination):
            raise ValueError('Keep output separate from every input')
    members = {name:path.read_bytes() for name,path in files(desktop)}
    for name in ('OIF.exe', 'OIF.exe.config', 'python/python.exe', 'desktop/WebView2Loader.dll',
                 'desktop/Microsoft.Web.WebView2.Core.dll', 'desktop/Microsoft.Web.WebView2.WinForms.dll',
                 'desktop/WebView2-LICENSE.txt'):
        if name not in members:
            raise ValueError('Incomplete Windows build: ' + name)
    tracked = release.git(ROOT,'ls-files','-z','harness').decode().split('\0')
    for path in filter(None,tracked):
        name = path[len('harness/'):]
        if name == '.gitignore':
            continue
        if members.get(name) != (ROOT/path).read_bytes():
            raise ValueError('Build differs from public source: ' + path)
    # Build-location strings must not enter generated scripts, metadata or code.
    private = [str(Path.home()), str(desktop), str(ROOT)]
    needles = [v.encode(enc) for v in private for enc in ('utf-8','utf-16le')]
    for name, raw in members.items():
        if any(needle in raw for needle in needles):
            raise ValueError('Local build location in packaged member: ' + name)
    members['application.json'] = plugin.encoded(dict(schema='oif-desktop-package-v1', **identity,
        members=[dict(path=n,bytes=len(b),sha256=plugin.digest(b)) for n,b in sorted(members.items())]))
    destination.mkdir()
    for item in manifest['artifacts']:
        raw=(base/item['name']).read_bytes()
        if plugin.digest(raw) != item['sha256']:
            raise ValueError('Core asset changed')
        (destination/item['name']).write_bytes(raw)
    app_name=f'OIF-Desktop-{version}-windows-x64.zip'
    archive(destination/app_name, f'OIF-Desktop-{version}', members)
    shots={p.name:p.read_bytes() for p in sorted((ROOT/'docs/assets').glob('oif-desktop-*.png'))}
    if len(shots) != 6 or len([name for name in shots if name.endswith('-ja.png')]) != 3:
        raise ValueError('The showcase needs three English and three Japanese images')
    if any(not raw.startswith(b'\x89PNG\r\n\x1a\n') for raw in shots.values()):
        raise ValueError('A showcase image is not encoded as PNG')
    for language, source in (('en', 'docs/showcase/README.md'), ('ja', 'docs/showcase/ja/README.md')):
        text = (ROOT/source).read_text(encoding='utf-8')
        image_prefix = '../assets/' if language == 'en' else '../../assets/'
        text = text.replace('](' + image_prefix, '](')
        old_link, new_link = ('ja/README.md', 'README-ja.md') if language == 'en' else ('../README.md', 'README.md')
        text = text.replace('](' + old_link + ')', '](' + new_link + ')')
        shots['README.md' if language == 'en' else 'README-ja.md'] = text.encode('utf-8')
    shots['LICENSE']=(ROOT/'LICENSE').read_bytes()
    archive(destination/f'OIF-Showcase-{version}.zip',f'OIF-Showcase-{version}',shots)
    manifest.update(profile='desktop-beta-v1', artifacts=[dict(name=p.name,bytes=p.stat().st_size,sha256=plugin.digest(p.read_bytes()))
        for p in sorted(destination.glob('*.zip'))])
    manifest['helpers'].append(dict(path='tools/desktop_release.py',sha256=plugin.digest(Path(__file__).read_bytes())))
    (destination/'release.json').write_bytes(plugin.encoded(manifest))
    (destination/'SHA256SUMS.txt').write_text(''.join(plugin.digest(p.read_bytes())+'  '+p.name+'\n'
        for p in sorted(destination.iterdir())),encoding='ascii')
    return dict(destination=str(destination), **manifest)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base','desktop','destination'):
        parser.add_argument('--'+name,type=lambda x:Path(x).resolve(),required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.base,args.desktop,args.destination),indent=2))
