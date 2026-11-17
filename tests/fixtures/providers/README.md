# Adapter contract samples

- `mymemory.json`: real response to public `hello`, English → Ukrainian, recorded 2026-11-17.
  Only fields consumed by the adapter remain; contribution IDs, author fields and timestamps were removed.
- Google GTX and Chrome samples: representative documented adapter shapes, **synthetic fixtures**.
  Live requests from this environment returned proxy HTTP 403, so these do not establish live service health.
- Google mobile HTML shapes are covered in `test_mobile_translation.py` (including missing containers,
  markup, entities and malformed pages). No user screenshots or library data are in these fixtures.

Tests verify parsing and fallback on incompatible shapes, with no network access. They cannot prevent
an unofficial service from changing its API or terms. Prefer DeepL, Google Cloud or a configured
LibreTranslate server when that stability matters.
