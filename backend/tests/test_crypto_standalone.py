import importlib.util
from pathlib import Path
import sys
import unittest

from cryptography.fernet import Fernet

MODULE_PATH = Path(__file__).parents[1] / "apps" / "common" / "services" / "crypto.py"
spec = importlib.util.spec_from_file_location("credential_crypto", MODULE_PATH)
credential_crypto = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = credential_crypto
spec.loader.exec_module(credential_crypto)


class CredentialCipherTests(unittest.TestCase):
    def test_encrypt_decrypt_and_wrong_key(self):
        key = Fernet.generate_key()
        cipher = credential_crypto.CredentialCipher(key)
        encrypted = cipher.encrypt("root-password")
        self.assertNotEqual(encrypted, "root-password")
        self.assertEqual(cipher.decrypt(encrypted), "root-password")

        with self.assertRaises(credential_crypto.CredentialCipherError):
            credential_crypto.CredentialCipher(Fernet.generate_key()).decrypt(encrypted)


if __name__ == "__main__":
    unittest.main()
