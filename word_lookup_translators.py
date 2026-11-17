"""Compatibility import; implementation lives in wordlookup.network.official."""
import sys
from wordlookup.network import official as _implementation
sys.modules[__name__] = _implementation
