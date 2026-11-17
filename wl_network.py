"""Compatibility import; implementation lives in wordlookup.network.budget."""
import sys
from wordlookup.network import budget as _implementation
sys.modules[__name__] = _implementation
