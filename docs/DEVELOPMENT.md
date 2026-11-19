# Розробка та перевірка

Python 3.11–3.13; еталонна Windows EXE-збірка — **CPython 3.12 x64**. Залежності закріплені у lock-файлах.
Програма зберігає дані поза каталогом вихідного коду. Для перевірок задавай тимчасовий `WORD_LOOKUP_DATA_DIR`.

## Перевірки

```sh
python -m pip install -r requirements.lock
python -m ruff check .
python -m mypy
python -m pytest -q --cov --cov-report=xml
python -m pip_audit --disable-pip --no-deps -r requirements.lock
```

Для UI на Linux потрібні Tk/Xvfb/Tesseract/xclip. `scripts/run_tests.sh` сам піднімає віртуальний дисплей
і ізолює конфіг. `tests_core` працює без дисплея, глобальних хуків та звернень до сховища ключів;
macOS CI запускає саме його. Справжні Windows Keychain/DPAPI-інтеграції поза Windows пропускаються.
Покриття вимірюється для коду програми з порогом 65%; воно не включає тести/скрипти.
Mypy спершу перевіряє десять модулів; це поступова типізація, а не заява про strict-типізацію всієї GUI.

Windows/Linux CI зберігають результати й PNG лише як артефакти. Windows-рендер при керованому
Tk-масштабі 100/125/150% **не дорівнює** фізичному DPI робочого столу. У діагностичному ZIP
на Windows окремо є `GetDpiForWindow`; автоматично перевірити всі монітори користувача неможливо.

## Збірка EXE

На чистому Windows з Python 3.12 x64:

```powershell
py -3.12 -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r requirements-build.lock
.\.build-venv\Scripts\python.exe scripts\build_exe.py --smoke
```

`WordLookup.spec` включає OCR-моделі, метадані версій, частотний словник, DPI-manifest і ліцензії.
Каталог `dist/WordLookup` містить EXE й залежності; його потрібно розповсюджувати цілком.
ZIP має стабільний порядок/час записів та SHA-256. Це відтворюване оточення збірки, не обіцянка
однакового PE-хешу на будь-яких версіях Windows/инструментів. CTranslate2/SentencePiece входять у EXE; моделі користувач імпортує окремо. Torch/Stanza/spaCy не встановлюються.

`--self-test <report.json>` — явний smoke-вхід, який ініціалізує реальний OCR на порожньому зображенні,
перевіряє Tk, словник, імпорт Windows-сховища та ліниве створення HTTP-сесії/наявність CA-файлу.
Він не читає ключі, не підключає глобальні хуки,
не звертається до мережі та не призупиняє процеси. `--pause-watchdog` обробляється легким launcher
до імпорту GUI; `word_lookup.py` лишається фасадом сумісності для старих інтеграцій/тестів.

Workflow збирає EXE для pull request і вручну. Публікація релізу відбувається лише для тегу,
який точно відповідає `__version__`; звичайний PR не публікує реліз.

### Час запуску EXE

`scripts/benchmark_startup.py` на Windows вимірює час від створення процесу до видимого
головного вікна, яке відповідає на `WM_NULL`. Кожен запуск має окремий порожній профіль;
скрипт завершує лише запущені ним процеси. Потрібні повністю розпаковані каталоги обох збірок.

```powershell
python scripts/benchmark_startup.py --before C:/old/WordLookup.exe --after C:/new/WordLookup.exe --runs 3 --output startup.json
```

Workflow `Compare real Windows EXE startup` збирає 8.5.0 та поточну версію з однаковими
залежностями й чергує порядок запусків на одному runner. У JSON є всі спроби та медіани.
Збірка/self-test прогріває файли: ці дані не підміняють запуск після перезавантаження,
антивірусну перевірку або ПК користувача. Звичайний `app.log` також містить час від входу
в Python-launcher до появи вікна та етапи імпорту/налаштувань/кешу; час до входу в Python
(зокрема OS/bootloader) входить лише у зовнішній EXE-замір.

## Lock-файли

Використовуй `scripts/compile_locks.py` з `uv` встановленої версії 0.12.23. Компіляція універсальна
для платформ і Python, з нижньою підтримуваною версією 3.11. Офлайн-lock містить лише CTranslate2/SentencePiece і їхні невеликі залежності; Torch, Stanza,
spaCy та CUDA/NVIDIA-пакетів у ньому немає. Переглядай diff і запускай pip-audit та CI після оновлення.

## Архітектура

`word_lookup.py` лише збирає об'єкти та експортує сумісний API. Контролери отримують
Settings, Library, CardCache, Providers, Ocr, Anki, журнал, події та діагностику явно.
Немає `configure(sys.modules[...])`, `runtime.os` чи імпорту фасаду з доменних сервісів.
Сумісні імена старих модулів мережі ведуть до `wordlookup/network/{budget,policy,official,providers}.py`.
`responses.py` і pooled requests.Session завантажуються при використанні HTTP; `policy.py`
перевіряє дозволи без завантаження HTTP-клієнта. Старий експорт `word_lookup.requests` лишається лінивим.
Нові поведінкові тести створюють окремі доменні об'єкти; старі fault-injection тести поступово мігрують.

## Локалізація

Статичні підписи проходять через gettext (`tr_ui`), ключі JSON і ID джерел не перекладаються.
`wordlookup/locale/wordlookup.pot` містить початковий каталог; `en/LC_MESSAGES/wordlookup.po`
поки частковий і призначений для розширення. Повний англійський інтерфейс не заявляється.

```sh
python scripts/compile_translations.py
WORD_LOOKUP_UI_LANGUAGE=en python word_lookup_launcher.py
```

Українська — стандартна мова й fallback. Після змін статичних підписів оновлюй каталог
через `scripts/extract_messages.py`. Динамічні повідомлення й старі частини UI переводяться поступово.
