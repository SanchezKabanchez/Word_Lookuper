# Third-party software and data

The original Word Lookup code is all rights reserved; see [LICENSE](LICENSE).
Dependencies retain their own licenses. The dependency versions and source projects are recorded in
`requirements.lock`, `requirements-build.lock` and the installed distribution metadata, which is
retained in the packaged EXE. PyInstaller's bootloader distribution exception applies to its loader.

The included English frequency data is separately **CC BY-SA 4.0**. Its source, attribution, unchanged
file hash and complete license are in [wordlookup/data/README.md](wordlookup/data/README.md).
RapidOCR's bundled detection/recognition models and its license are included from the pinned
`rapidocr-onnxruntime` distribution (Apache-2.0). ONNX Runtime is MIT; NumPy is BSD-3-Clause;
OpenCV is Apache-2.0; Pygame is LGPL-2.1; Pillow is HPND; Requests is Apache-2.0;
psutil is BSD-3-Clause; pynput is LGPL-3.0; genanki is MIT; keyring is MIT; mss is MIT; pyperclip is BSD-3-Clause.
Consult the bundled `THIRD_PARTY_LICENSES` directory for the exact upstream texts.

The optional local runtime uses CTranslate2 (MIT) and SentencePiece (Apache-2.0) with compatible
Argos-format SentencePiece model files. The Argos Python distribution is not bundled or installed.
Translation models retain their own licenses; check each model license before installing it.
Google Cloud, DeepL, LibreTranslate and other remote services are not bundled software.
