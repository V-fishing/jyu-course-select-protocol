"""敏感存储数据（Cookie）的可选对称加密。

优先使用 ``cryptography``；未安装时回退到 ``pycryptodomex`` (AES-GCM)。
"""

from __future__ import annotations

import base64
import os

from course_grabber.config import Config

try:
    from cryptography.fernet import Fernet, InvalidToken

    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False
    from Cryptodome.Cipher import AES
    from Cryptodome.Protocol.KDF import scrypt


class CookieCrypto:
    """当 ENCRYPT_COOKIES 启用时，对 Cookie 字符串进行加解密。"""

    def __init__(self, key: str | None = None):
        raw_key = (key or Config.COOKIE_KEY or "").strip()
        self._enabled = Config.ENCRYPT_COOKIES and bool(raw_key)
        self._key = raw_key.encode() if self._enabled else None

    def is_enabled(self) -> bool:
        return self._enabled

    def encrypt(self, plaintext: str) -> str:
        if not self._enabled or not plaintext:
            return plaintext
        if _HAS_CRYPTOGRAPHY:
            return Fernet(self._key).encrypt(plaintext.encode()).decode()
        return self._encrypt_aes_gcm(plaintext)

    def decrypt(self, ciphertext: str) -> str:
        if not self._enabled or not ciphertext:
            return ciphertext
        try:
            if _HAS_CRYPTOGRAPHY:
                return Fernet(self._key).decrypt(ciphertext.encode()).decode()
            return self._decrypt_aes_gcm(ciphertext)
        except Exception:
            # 假设原值就是明文
            return ciphertext

    # ---- pycryptodomex 回退 ----
    def _encrypt_aes_gcm(self, plaintext: str) -> str:
        salt = os.urandom(16)
        key = scrypt(self._key, salt, key_len=32, N=2**14, r=8, p=1)
        cipher = AES.new(key, AES.MODE_GCM)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode())
        payload = salt + cipher.nonce + tag + ciphertext
        return "enc:" + base64.urlsafe_b64encode(payload).decode()

    def _decrypt_aes_gcm(self, token: str) -> str:
        if not token.startswith("enc:"):
            raise ValueError("不是加密令牌")
        raw = base64.urlsafe_b64decode(token[4:].encode())
        salt, nonce, tag, ciphertext = raw[:16], raw[16:28], raw[28:44], raw[44:]
        key = scrypt(self._key, salt, key_len=32, N=2**14, r=8, p=1)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode()


def generate_key() -> str:
    """生成一个 URL 安全的 base64 编码 32 字节密钥。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


if __name__ == "__main__":
    print("后端:", "cryptography" if _HAS_CRYPTOGRAPHY else "pycryptodomex")
    print("生成新密钥:", generate_key())
    test_cookie = "JSESSIONID=ABC123; route=xyz"
    crypto = CookieCrypto()
    print("加密已启用:", crypto.is_enabled())
    if crypto.is_enabled():
        enc = crypto.encrypt(test_cookie)
        dec = crypto.decrypt(enc)
        print("原始:", test_cookie)
        print("加密后:", enc)
        print("解密后:", dec)
        assert dec == test_cookie
