"""Smoke tests for the installable package shell."""

import unittest


class PackageImportTest(unittest.TestCase):
    def test_package_imports(self) -> None:
        import cmw

        self.assertEqual(cmw.__name__, "cmw")


if __name__ == "__main__":
    unittest.main()
