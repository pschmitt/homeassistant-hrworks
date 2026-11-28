"""The rbw credential provider of the CLI."""

import json
import unittest
from unittest.mock import patch

from hrworks.config import credentials


class RbwCredentialsTest(unittest.TestCase):
    def company(self, *fields):
        item = {"data": {"username": "user", "password": "secret"}, "fields": list(fields)}
        with patch("subprocess.check_output", return_value=json.dumps(item)):
            return credentials({"rbw_entry": "entry"}).get("company_id")

    def test_company_id_field_names(self):
        for name in ("Company ID", "company id", "company", "Company"):
            self.assertEqual(self.company({"name": name, "value": "acme"}), "acme", name)

    def test_company_id_wins_over_company(self):
        self.assertEqual(
            self.company(
                {"name": "company", "value": "other"},
                {"name": "Company ID", "value": "acme"},
            ),
            "acme",
        )

    def test_without_a_company_field(self):
        self.assertIsNone(self.company({"name": None, "value": "x"}))


if __name__ == "__main__":
    unittest.main()
