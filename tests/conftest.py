"""Test configuration.

Point ZettaBrain at a throwaway data directory before anything imports the package.
`config.BASE_DIR` is resolved at import time, so this must run first — pytest loads
conftest before collecting test modules.

Without this the engine tests read the real price list database, so they pass on a
fresh machine and fail on any machine that has actually ingested a price list.
"""

import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="zettabrain-test-")
os.environ["ZETTABRAIN_LITE_DIR"] = _TEST_DATA_DIR
