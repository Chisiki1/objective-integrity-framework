"""OpenRouter's documented localhost PKCE flow; no provider passwords in OIF."""
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

from .settings import SettingsError


class ProviderLogin:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport
        self.pending = {}

    def begin(self, origin):
        self.pending = {k: v for k, v in self.pending.items() if v['expires'] > time.monotonic()}
        if len(self.pending) >= 8:
            raise SettingsError('ログイン画面を開きすぎています。開いている画面で続けてください。')
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        self.pending[state] = {'verifier': verifier, 'expires': time.monotonic() + 600}
        callback = origin + '/oauth/openrouter/' + state
        return {'url': 'https://openrouter.ai/auth?' + urlencode({
            'callback_url': callback, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}

    async def finish(self, state, code):
        pending = self.pending.pop(state, None)
        if not pending or pending['expires'] <= time.monotonic():
            raise SettingsError('ログインの有効時間が切れたか、すでに処理済みです。接続設定から開き直してください。')
        if not isinstance(code, str) or not 1 <= len(code) <= 4096 or any(ord(c) < 32 for c in code):
            raise SettingsError('認証コードを確認できません。接続設定からログインし直してください。')
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False, timeout=30) as client:
                async with client.stream('POST', 'https://openrouter.ai/api/v1/auth/keys', json={
                        'code': code, 'code_verifier': pending['verifier'], 'code_challenge_method': 'S256'}) as response:
                    if response.status_code != 200:
                        raise SettingsError('OpenRouterの認証が完了しませんでした。接続設定からログインし直してください。')
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part)
                        if len(raw) > 65536:
                            raise SettingsError('認証サービスからの応答が大きすぎます。')
            import json
            key = json.loads(raw).get('key')
            if not isinstance(key, str) or not key:
                raise ValueError()
            # Do not replace the active provider or interrupt an existing task.
            return self.settings.save_profile('OpenRouter', credential=key, values={
                'base_url': 'https://openrouter.ai/api/v1', 'auth_method': 'openrouter_oauth'})
        except (httpx.HTTPError, ValueError, TypeError, OSError):
            raise SettingsError('認証結果を保存できませんでした。現在の接続は保持しています。接続設定からやり直してください。') from None
