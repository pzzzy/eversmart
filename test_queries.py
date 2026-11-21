import re
import unittest
from pathlib import Path


class BundledQueryTests(unittest.TestCase):
    def test_catalog_contains_only_parameterized_application_operations(self):
        import queries

        source = Path(__file__).with_name('eversmart.py').read_text()
        used = set(re.findall(r'catalog_query\("([A-Za-z0-9_]+)"', source))
        self.assertTrue(used)
        self.assertEqual(set(queries.QUERIES), used)
        for name, query in queries.QUERIES.items():
            with self.subTest(name=name):
                self.assertRegex(query, rf'^\s*(?:query|mutation)\s+{name}\s*\(')
                self.assertNotRegex(query, r'\b\d{8,}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-|[\'\"]')


if __name__ == '__main__':
    unittest.main()
