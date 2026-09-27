"""Attachment parsing keeps bytes private and source content below instructions."""
import base64
import io
import struct
import zipfile
from xml.etree import ElementTree

from .models import PolicyError
from .store import redact

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 12


def decode_files(values):
    if not isinstance(values, list) or len(values) > MAX_FILES:
        raise PolicyError('添付ファイルは一度に12件までです。')
    files = []
    for item in values:
        name = item.get('filename')
        if (not isinstance(name, str) or not name.strip() or len(name) > 255 or name in {'.', '..'}
                or any(c in name for c in '/\\:') or any(ord(c) < 32 for c in name)):
            raise PolicyError('添付ファイル名が正しくありません。')
        try:
            raw = base64.b64decode(item['base64'], validate=True)
        except (ValueError, TypeError, KeyError):
            raise PolicyError('添付ファイルを読み取れませんでした。もう一度選択してください。') from None
        if len(raw) > MAX_FILE_BYTES:
            raise PolicyError('添付ファイルは1件10 MiB以下にしてください。')
        files.append({'filename': name, 'bytes': raw})
    if sum(len(f['bytes']) for f in files) > MAX_TOTAL_BYTES:
        raise PolicyError('一度に送信する添付は合計20 MiB以下にしてください。')
    return files


def image_type(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n') and len(raw) >= 24:
        w, h = struct.unpack('>II', raw[16:24])
        if 0 < w <= 16000 and 0 < h <= 16000:
            return 'image/png'
    if raw.startswith(b'\xff\xd8\xff') and raw.endswith(b'\xff\xd9'):
        return 'image/jpeg'
    if len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return None


def source_view(raw, filename):
    mime = image_type(raw)
    if mime:
        return {'format': 'image', 'mime': mime, 'text': filename + '：添付画像。画像入力は対応モデルで確認できます。'}
    try:
        text = raw.decode('utf-16') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode('utf-8-sig')
        if '\x00' in text:
            raise UnicodeError()
        return {'format': 'text', 'text': redact(text)}
    except UnicodeError:
        pass
    if filename.lower().endswith('.docx'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                info = archive.getinfo('word/document.xml')
                if info.file_size > 4 * 1024 * 1024:
                    raise ValueError('document too large')
                xml = archive.read(info)
                if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:
                    raise ValueError('external XML declaration')
                root = ElementTree.fromstring(xml)
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text = '\n'.join(''.join(p.itertext()) for p in root.findall('.//w:p', ns))
                return {'format': 'docx', 'text': redact(text), 'limits': '本文の文字を抽出。画像と配置は未確認。'}
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
            return {'format': 'unsupported', 'text': filename + '：文書の本文を抽出できませんでした。'}
    if raw.startswith(b'%PDF-'):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > 200:
                return {'format': 'unsupported', 'text': filename + '：必要なページを分けて添付してください（200ページを超えています）。'}
            text = '\n\n'.join(page.extract_text() or '' for page in reader.pages)
            return {'format': 'pdf', 'text': redact(text), 'limits': 'PDFの文字を抽出。画像やスキャン本文は未確認。'}
        except ImportError:
            return {'format': 'unsupported', 'text': filename + '：この環境ではPDF本文の読取りが未接続です。テキストを添えてください。'}
        except Exception:
            return {'format': 'unsupported', 'text': filename + '：PDF本文を抽出できませんでした。画像またはテキストを添えてください。'}
    return {'format': 'unsupported', 'text': filename + '：ファイルは保存済みですが、この形式の内容をまだ読み取れません。内容を推測しないでください。'}


def clipboard_files():
    """Explicit local paste fallback for Explorer CF_HDROP; no polling or writes."""
    import ctypes
    import os
    from ctypes import wintypes
    from pathlib import Path
    from .capabilities import check_node
    if os.name != 'nt':
        return []
    user = ctypes.WinDLL('user32', use_last_error=True)
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.OpenClipboard.restype = wintypes.BOOL
    user.GetClipboardData.argtypes = [wintypes.UINT]
    user.GetClipboardData.restype = wintypes.HANDLE
    shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell.DragQueryFileW.restype = wintypes.UINT
    if not user.OpenClipboard(None):
        raise PolicyError('クリップボードが使用中です。もう一度貼り付けてください。')
    try:
        handle = user.GetClipboardData(15)
        if not handle:
            return []
        count = shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        if count > MAX_FILES:
            raise PolicyError('一度に貼り付けるファイルは12件までです。')
        files, size = [], 0
        for index in range(count):
            length = shell.DragQueryFileW(handle, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            shell.DragQueryFileW(handle, index, buffer, length + 1)
            path = Path(buffer.value)
            if path.is_dir():
                raise PolicyError('フォルダー内のファイルを選択して貼り付けてください。')
            for parent in reversed(path.parents):
                check_node(parent)
            check_node(path)
            if path.stat().st_size > MAX_FILE_BYTES:
                raise PolicyError('貼り付けるファイルは1件10 MiB以下にしてください。')
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            size += len(raw)
            if len(raw) > MAX_FILE_BYTES or size > MAX_TOTAL_BYTES:
                raise PolicyError('貼り付けるファイルは1件10 MiB、合計20 MiB以下にしてください。')
            files.append({'filename': path.name, 'base64': base64.b64encode(raw).decode()})
        return files
    finally:
        user.CloseClipboard()
