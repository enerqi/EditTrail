"""Minimal stub for the UnitTesting Sublime package, which is not distributed on PyPI.

Only what ``st_tests`` imports. Without it the base class resolves to Unknown, and an Unknown base
makes every ``self.view`` and ``self.assert*`` in a test body unchecked: a whole layer of tests that
looks type-checked and is not.
"""

import unittest

class DeferrableTestCase(unittest.TestCase): ...
