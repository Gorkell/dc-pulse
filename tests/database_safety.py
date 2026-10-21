"""Refuse destructive database tests outside an explicitly enabled local fixture."""
import os
from urllib.parse import urlsplit


def validate_test_database(url):
    parsed=urlsplit(url)
    if (os.environ.get('ALLOW_TEST_DATABASE_RESET')!='1'
            or parsed.scheme not in ('postgresql','postgres')
            or parsed.hostname not in ('localhost','127.0.0.1','::1')
            or parsed.path!='/iot_test'
            or parsed.username!='test'
            or parsed.query or parsed.fragment):
        raise RuntimeError(
            'Refusing schema reset: only a disposable local PostgreSQL database '
            'iot_test owned by test is allowed, with ALLOW_TEST_DATABASE_RESET=1. '
            'Neon and all external/production databases are prohibited.')
    return url
