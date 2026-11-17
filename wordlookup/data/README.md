# Bundled English word frequencies

`en_frequency_50k.txt` is the unmodified English 50,000-token list from
[Hermit Dave's FrequencyWords](https://github.com/hermitdave/FrequencyWords),
generated from the [OpenSubtitles 2018 corpus](https://opus.nlpl.eu/OpenSubtitles2018.php).

- Author / attribution: Hermit Dave and the OpenSubtitles contributors.
- Source revision: `525f9b560de45753a5ea01069454e72e9aa541c6`.
- [Exact source file](https://github.com/hermitdave/FrequencyWords/blob/525f9b560de45753a5ea01069454e72e9aa541c6/content/2018/en/en_50k.txt).
- SHA-256: `5351ff405b1126ef555791dd4d9798a48e3e9a501a9fc481a9da957752cfb458`.
- License: **Creative Commons Attribution-ShareAlike 4.0 International**,
  as declared in the upstream project's README. The complete license is in
  [LICENSE-CC-BY-SA-4.0.txt](LICENSE-CC-BY-SA-4.0.txt).
  License URL: <https://creativecommons.org/licenses/by-sa/4.0/>.
- Changes: none to the frequency file. The application filters tokens when
  loading and computes ranks and broad frequency bands at runtime.

Retain this attribution and license when distributing the data. Modified
versions of this data must also follow the attribution and share-alike terms.
The license applies to this data, independently of the application's code.

Each line contains a token and its number of appearances in the corpus.
Subtitle frequency is not a CEFR level: the application displays a frequency
badge, never an inferred A1–C2 classification. Proper names, slang, and OCR-like
spellings can occur in subtitles, so spelling suggestions use conservative
frequency and ambiguity checks. They remain suggestions, not a dictionary
guarantee. No translation definitions are included in this frequency list.

`word_lookup_lexicon.py` also moves the project's existing irregular-form facts
into a local module; these are independent of the subtitle frequency data.
