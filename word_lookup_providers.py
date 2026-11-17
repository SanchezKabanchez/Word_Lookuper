"""Compatibility import; implementation lives in wordlookup.network.providers."""
import sys
from wordlookup.network import providers as _implementation
sys.modules[__name__] = _implementation
