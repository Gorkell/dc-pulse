import os
import unittest
from unittest.mock import patch
from database_safety import validate_test_database


class DatabaseSafetyTests(unittest.TestCase):
    def test_explicit_local_disposable_database(self):
        with patch.dict(os.environ,{'ALLOW_TEST_DATABASE_RESET':'1'}):
            url='postgresql://test:disposable@localhost:5432/iot_test'
            self.assertEqual(validate_test_database(url),url)

    def test_requires_reset_opt_in(self):
        with patch.dict(os.environ,{'ALLOW_TEST_DATABASE_RESET':'0'}):
            with self.assertRaises(RuntimeError):
                validate_test_database('postgresql://test:disposable@localhost/iot_test')

    def test_rejects_neon_production_and_connection_overrides(self):
        with patch.dict(os.environ,{'ALLOW_TEST_DATABASE_RESET':'1'}):
            for url in (
                'postgresql://test:example@ep-demo.neon.tech/iot_test',
                'postgresql://test:example@database.example/iot_test',
                'postgresql://test:example@localhost/production',
                'postgresql://owner:example@localhost/iot_test',
                'postgresql://test:example@localhost/iot_test?host=ep-demo.neon.tech',
            ):
                with self.subTest(url=url),self.assertRaises(RuntimeError):
                    validate_test_database(url)
