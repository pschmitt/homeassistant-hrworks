"""Published RFC vectors; no employee credentials."""

import base64
import unittest

from hrworks import totp

URI = "otpauth://totp/Example:test?secret=JBSWY3DPEHPK3PXP"


class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        vectors = [
            (59, "94287082", "46119246", "90693936"),
            (1111111109, "07081804", "68084774", "25091201"),
            (1111111111, "14050471", "67062674", "99943326"),
            (1234567890, "89005924", "91819424", "93441116"),
            (2000000000, "69279037", "90698825", "38618901"),
            (20000000000, "65353130", "77737706", "47863826"),
        ]
        for algorithm, secret, index in [
            ("SHA1", b"12345678901234567890", 1),
            ("SHA256", b"12345678901234567890123456789012", 2),
            ("SHA512", b"1234567890123456789012345678901234567890123456789012345678901234", 3),
        ]:
            uri = f"otpauth://totp/test?secret={base64.b32encode(secret).decode()}&algorithm={algorithm}&digits=8"
            for vector in vectors:
                with self.subTest(algorithm=algorithm, timestamp=vector[0]):
                    self.assertEqual(totp.totp_code(uri, timestamp=vector[0]), vector[index])

    def test_period_and_padding(self):
        self.assertEqual(len(totp.totp_code(URI, timestamp=0)), 6)
        self.assertEqual(totp.totp_code(URI, timestamp=0), totp.totp_code(URI, timestamp=29))
        self.assertNotEqual(totp.totp_code(URI, timestamp=29), totp.totp_code(URI, timestamp=30))

    def test_invalid_uris_hide_the_secret(self):
        for uri in [
            "https://example.com/secret",
            URI.replace("totp/", "hotp/"),
            "otpauth://totp/test",
            URI + "&secret=OTHER",
            URI + "&digits=7",
            URI + "&period=0",
            URI + "&algorithm=MD5",
            URI + "#secret",
            URI.replace("JBSWY3DPEHPK3PXP", "!!"),
        ]:
            with self.subTest(uri=uri), self.assertRaises(totp.InvalidTotp) as context:
                totp.totp_code(uri, timestamp=0)
            self.assertEqual(str(context.exception), "invalid_totp")
