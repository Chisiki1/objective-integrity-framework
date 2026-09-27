"""Explicit provider configuration; credentials are write-only and DPAPI protected."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .models import ConfigurationRequired


class SettingsError(ValueError):
    """An invalid setting, without echoing a potentially sensitive value."""


class _WindowsDPAPI:
    """Current Windows user scope. This does not defend against that same user."""

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def __init__(self) -> None:
        if os.name != "nt":
            raise ConfigurationRequired("Secure persistent credentials require Windows DPAPI on this host.")
        self.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p
        self.crypt32.CryptProtectData.argtypes = [ctypes.POINTER(self.Blob), wintypes.LPCWSTR, ctypes.POINTER(self.Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(self.Blob)]
        self.crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(self.Blob), ctypes.c_void_p, ctypes.POINTER(self.Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(self.Blob)]
        self.crypt32.CryptProtectData.restype = self.crypt32.CryptUnprotectData.restype = wintypes.BOOL

    def _transform(self, raw: bytes, *, protect: bool) -> bytes:
        buffer = ctypes.create_string_buffer(raw)
        source = self.Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        entropy_buffer = ctypes.create_string_buffer(b"HermesPolicyHarness/credentials/v1")
        entropy = self.Blob(len(entropy_buffer.raw) - 1, ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = self.Blob()
        fn = self.crypt32.CryptProtectData if protect else self.crypt32.CryptUnprotectData
        description = "Hermes Policy Harness" if protect else None
        if not fn(ctypes.byref(source), description, ctypes.byref(entropy), None, None, 1, ctypes.byref(result)):
            raise ConfigurationRequired(f"Windows credential protection failed (code {ctypes.get_last_error()}).")
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            self.kernel32.LocalFree(result.pbData)

    def protect(self, raw: bytes) -> bytes:
        return self._transform(raw, protect=True)

    def unprotect(self, raw: bytes) -> bytes:
        return self._transform(raw, protect=False)


DEFAULTS: dict[str, Any] = {
    "base_url": "", "model": "", "review_model": "", "api_mode": "compatible",
    "model_context_tokens": None, "max_output_tokens": None, "reasoning_effort": None,
    "web_provider": "none", "web_base_url": None,
    "request_timeout_seconds": 300.0,
    "user_agent": "hermes-policy-harness/0.1.0",
    "web_max_response_bytes": 2_000_000,
    "auth_method": "api_key",
}
SECRET_NAMES = frozenset({"model_api_key", "web_api_key"})
MODEL_FIELDS = frozenset({'base_url', 'model', 'review_model', 'api_mode', 'auth_method',
                         'model_context_tokens', 'max_output_tokens', 'reasoning_effort', 'request_timeout_seconds'})


def _https_base(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment
        parsed.port
    except ValueError:
        valid = False
    if not valid or any(ord(c) < 32 for c in value):
        raise SettingsError(f"{name} must be an HTTPS URL without credentials, query or fragment.")
    return value.rstrip("/")


class SettingsManager:
    def __init__(self, data_dir: Path, *, protector=None) -> None:
        self.data_dir = Path(data_dir)
        if self.data_dir.is_symlink() or (self.data_dir.exists() and not self.data_dir.is_dir()):
            raise SettingsError("The settings directory must be a plain directory.")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "provider-settings.json"
        self._protector = protector
        self._lock = threading.RLock()

    def _cipher(self):
        if self._protector is None:
            self._protector = _WindowsDPAPI()
        return self._protector

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 2, "values": dict(DEFAULTS), "secrets": {}, "redaction_history": {}}
        if self.path.is_symlink() or not self.path.is_file():
            raise SettingsError("The settings file must be a plain file.")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document.get("version") not in (1, 2) or not isinstance(document.get("values"), dict) or not isinstance(document.get("secrets"), dict):
                raise ValueError()
            if set(document["values"]) - set(DEFAULTS) or set(document["secrets"]) - SECRET_NAMES:
                raise ValueError()
            history = document['redaction_history'] if document['version'] == 2 else document.get('redaction_history', {})
            if not isinstance(history, dict) or set(history) - SECRET_NAMES:
                raise ValueError()
            if any(not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items)
                   for items in history.values()):
                raise ValueError()
            if any(not isinstance(value, str) for value in document["secrets"].values()):
                raise ValueError()
            document["redaction_history"] = history
            document["values"] = self._validate({**DEFAULTS, **document["values"]})
            profiles = document.get('model_profiles', {})
            if not isinstance(profiles, dict) or len(profiles) > 100:
                raise ValueError()
            for identity, profile in profiles.items():
                if (not isinstance(identity, str) or not isinstance(profile, dict)
                        or not isinstance(profile.get('name'), str) or not 1 <= len(profile['name']) <= 80
                        or set(profile.get('values', {})) != MODEL_FIELDS
                        or not isinstance(profile.get('credential', ''), str)):
                    raise ValueError()
                self._validate({**DEFAULTS, **profile['values']})
            if document.get('active_profile') is not None and document['active_profile'] not in profiles:
                raise ValueError()
            return document
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise SettingsError("Stored provider settings are invalid; preserve and repair the configuration.") from None

    @staticmethod
    def _validate(values: dict) -> dict:
        result = dict(values)
        for name in ("base_url", "model", "review_model", "user_agent"):
            if not isinstance(result[name], str) or any(ord(c) < 32 for c in result[name]):
                raise SettingsError(f"Invalid setting: {name}")
            result[name] = result[name].strip()
        if result["base_url"]:
            result["base_url"] = _https_base(result["base_url"], "base_url")
        if result["web_base_url"]:
            if not isinstance(result["web_base_url"], str):
                raise SettingsError("Invalid setting: web_base_url")
            result["web_base_url"] = _https_base(result["web_base_url"], "web_base_url")
        if result["api_mode"] not in ("compatible", "deepagents"):
            raise SettingsError("Invalid setting: api_mode")
        if result['auth_method'] not in ('api_key', 'openrouter_oauth'):
            raise SettingsError('Invalid setting: auth_method')
        if result['auth_method'] == 'openrouter_oauth' and result['base_url'] != 'https://openrouter.ai/api/v1':
            raise SettingsError('OpenRouter login can only be used with OpenRouter.')
        if result["web_provider"] not in ("brave", "tavily", "public_url", "none"):
            raise SettingsError("Invalid setting: web_provider")
        for name in ("model_context_tokens", "max_output_tokens"):
            value = result[name]
            if value is not None and (type(value) is not int or value <= 0):
                raise SettingsError(f"Invalid setting: {name}")
        if result["model_context_tokens"] and result["max_output_tokens"] and result["max_output_tokens"] >= result["model_context_tokens"]:
            raise SettingsError("max_output_tokens must be smaller than model_context_tokens.")
        if result["reasoning_effort"] is not None and (not isinstance(result["reasoning_effort"], str) or not result["reasoning_effort"].strip() or any(ord(c) < 32 for c in result["reasoning_effort"])):
            raise SettingsError("Invalid setting: reasoning_effort")
        if isinstance(result["request_timeout_seconds"], bool) or not isinstance(result["request_timeout_seconds"], (int, float)) or not 0 < result["request_timeout_seconds"] <= 86_400:
            raise SettingsError("Invalid setting: request_timeout_seconds")
        if type(result["web_max_response_bytes"]) is not int or result["web_max_response_bytes"] <= 0:
            raise SettingsError("Invalid setting: web_max_response_bytes")
        if not result["user_agent"] or result["user_agent"].lower().startswith(("python-", "httpx", "openai", "langchain")):
            raise SettingsError("Set a distinct application user_agent.")
        return result

    @staticmethod
    def _public(document: dict) -> dict:
        return {**document["values"], "model_api_key_configured": bool(document["secrets"].get("model_api_key")), "web_api_key_configured": bool(document["secrets"].get("web_api_key")), "secret_storage": "windows-dpapi-current-user", "cost_limit": None}

    def get(self) -> dict:
        """Return configuration, never decrypted credentials."""
        with self._lock:
            return self._public(self._load())

    def public(self) -> dict:
        return self.get()

    def secret(self, name: str) -> str | None:
        if name not in SECRET_NAMES:
            raise SettingsError("Unknown credential field.")
        with self._lock:
            value = self._load()["secrets"].get(name)
            if not value:
                return None
            return self._decode(value)

    def _decode(self, value: str) -> str:
        try:
            return self._cipher().unprotect(base64.b64decode(value, validate=True)).decode("utf-8")
        except (ValueError, TypeError, OSError, ConfigurationRequired):
            raise ConfigurationRequired("The credential cannot be decrypted by this Windows user.") from None

    def redaction_secrets(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Internal redaction input and unavailable field names; never an API response.

        Current field names and history:<field> markers identify unavailable
        values. Replacement never resolves an unreadable historical value.
        All retained values are decrypted again on each read, so restoring the
        original Windows protection context restores redaction after restart.
        """
        with self._lock:
            document = self._load()
            values, unavailable = [], []
            for name in sorted(SECRET_NAMES):
                entries = [(name, document["secrets"].get(name))]
                entries += [('history:' + name, value) for value in document["redaction_history"].get(name, [])]
                for identity, ciphertext in entries:
                    if not ciphertext:
                        continue
                    try:
                        value = self._decode(ciphertext)
                    except ConfigurationRequired:
                        if identity not in unavailable:
                            unavailable.append(identity)
                    else:
                        if value and value not in values:
                            values.append(value)
            return tuple(values), tuple(unavailable)

    def _preserve_settings(self, raw: bytes) -> None:
        """Keep the exact encrypted preimage before replacing current settings."""
        directory = self.data_dir / 'settings-history'
        if directory.is_symlink() or (hasattr(directory, 'is_junction') and directory.is_junction()):
            raise SettingsError('Settings history must be a private plain directory.')
        directory.mkdir(mode=0o700, exist_ok=True)
        destination = directory / (hashlib.sha256(raw).hexdigest() + '.json')
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file() or destination.stat().st_nlink != 1 or destination.read_bytes() != raw:
                raise SettingsError('Settings history differs; preserve and reconcile the original files.')
            return
        with destination.open('xb') as target:
            os.chmod(destination, 0o600)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())

    def update(self, values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - set(DEFAULTS) - SECRET_NAMES:
            raise SettingsError("Unknown provider setting; submitted values were not saved.")
        with self._lock:
            document = self._load()
            before = self.path.read_bytes() if self.path.exists() else None
            old_url, new_url = document['values']['base_url'], values.get('base_url', document['values']['base_url'])
            if old_url and urlsplit(old_url).netloc != urlsplit(new_url).netloc and 'model_api_key' not in values:
                raise SettingsError('接続先を変更する場合は、その接続先のAPIキーも入力してください。保存した接続の切替も利用できます。')
            if 'model_api_key' in values and 'auth_method' not in values:
                values = dict(values, auth_method='api_key')
            document["values"] = self._validate({**document["values"], **{k: v for k, v in values.items() if k in DEFAULTS}})
            for name in SECRET_NAMES & values.keys():
                value = values[name]
                prior = document["secrets"].get(name)
                if prior:
                    history = document['redaction_history'].setdefault(name, [])
                    if prior not in history:
                        history.append(prior)
                if value is None or value == "":
                    document["secrets"].pop(name, None)
                    continue
                if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                    raise SettingsError("Invalid credential value.")
                try:
                    protected = self._cipher().protect(value.encode("utf-8"))
                except (OSError, ConfigurationRequired):
                    raise ConfigurationRequired('The replacement credential could not be protected; settings were not changed.') from None
                document["secrets"][name] = base64.b64encode(protected).decode("ascii")
            document['version'] = 2
            identity = document.get('active_profile')
            if identity:
                profile = document['model_profiles'][identity]
                if (profile['values'] != {k: document['values'][k] for k in MODEL_FIELDS}
                        or profile['credential'] != document['secrets'].get('model_api_key', '')):
                    # Named connections are snapshots. Preparing a second one
                    # must not overwrite the first provider/key before Save As.
                    document['active_profile'] = None
            self._write(document, before)
            return self._public(document)

    def _write(self, document, before):
        if before is not None:
            self._preserve_settings(before)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open('x', encoding='utf-8', newline='\n') as target:
                os.chmod(temporary, 0o600)
                json.dump(document, target, ensure_ascii=False, indent=2, allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def profiles(self):
        with self._lock:
            document = self._load()
            return {'active': document.get('active_profile'), 'profiles': [
                {'id': identity, 'name': profile['name'], **profile['values'],
                 'credential_configured': bool(profile.get('credential'))}
                for identity, profile in document.get('model_profiles', {}).items()]}

    def save_profile(self, name, *, values=None, credential=None):
        """Save current settings, or a newly authorized OAuth connection, privately."""
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise SettingsError('接続名は1〜80文字で指定してください。')
        with self._lock:
            document = self._load()
            before = self.path.read_bytes() if self.path.exists() else None
            profiles = document.setdefault('model_profiles', {})
            if len(profiles) >= 100:
                raise SettingsError('接続設定は100件まで保存できます。')
            chosen = self._validate({**DEFAULTS, **values}) if values is not None else document['values']
            encrypted = document['secrets'].get('model_api_key', '')
            if credential is not None:
                if not isinstance(credential, str) or not credential or any(ord(c) < 32 for c in credential):
                    raise SettingsError('Invalid credential value.')
                encrypted = base64.b64encode(self._cipher().protect(credential.encode('utf-8'))).decode('ascii')
                # Redaction remains possible even before this profile is selected.
                history = document['redaction_history'].setdefault('model_api_key', [])
                if encrypted not in history:
                    history.append(encrypted)
            identity = uuid4().hex
            profiles[identity] = {'name': name.strip(), 'values': {k: chosen[k] for k in MODEL_FIELDS}, 'credential': encrypted}
            if values is None:
                document['active_profile'] = identity
            self._write(document, before)
            return identity

    def activate_profile(self, identity):
        with self._lock:
            document = self._load()
            profile = document.get('model_profiles', {}).get(identity)
            if not profile:
                raise SettingsError('接続設定が見つかりません。')
            before = self.path.read_bytes()
            previous = document['secrets'].get('model_api_key')
            history = document['redaction_history'].setdefault('model_api_key', [])
            if previous and previous not in history:
                history.append(previous)
            document['values'].update(profile['values'])
            if profile['credential']:
                document['secrets']['model_api_key'] = profile['credential']
            else:
                document['secrets'].pop('model_api_key', None)
            document['active_profile'] = identity
            self._write(document, before)
            return self._public(document)

    def delete_profile(self, identity):
        with self._lock:
            document = self._load()
            if identity not in document.get('model_profiles', {}):
                raise SettingsError('接続設定が見つかりません。')
            before = self.path.read_bytes()
            profile = document['model_profiles'].pop(identity)
            history = document['redaction_history'].setdefault('model_api_key', [])
            if profile['credential'] and profile['credential'] not in history:
                history.append(profile['credential'])
            if document.get('active_profile') == identity:
                document['active_profile'] = None
            self._write(document, before)
