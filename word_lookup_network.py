"""Compatibility import; implementation lives in wordlookup.network.policy."""
import sys
from wordlookup.network import policy as _implementation
sys.modules[__name__] = _implementation
