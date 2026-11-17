"""Local English frequency metadata and conservative OCR spelling suggestions.

The bundled subtitle frequency data is CC BY-SA 4.0; see wordlookup/data/README.md.
Frequency ranks describe corpus usage, not CEFR proficiency levels.
This module deliberately has no network or application/UI dependencies.
"""
from functools import lru_cache
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
import string

_manual_word = ContextVar("word_lookup_manual_word", default=False)


@contextmanager
def manual_word_scope():
    """A user's typed correction is authoritative across lookup executor tasks."""
    token = _manual_word.set(True)
    try:
        yield
    finally:
        _manual_word.reset(token)


def manual_word_active():
    return _manual_word.get()


_DATA_PATH = Path(__file__).resolve().parent / "wordlookup" / "data" / "en_frequency_50k.txt"
_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?\Z")

_IRREGULAR_SPEC = """
be: was were been
beat: beaten
become: became
begin: began begun
bend: bent
bind: bound
bite: bit bitten
bleed: bled
blow: blew blown
break: broke broken
breed: bred
bring: brought
build: built
burn: burnt
buy: bought
catch: caught
choose: chose chosen
cling: clung
come: came
creep: crept
deal: dealt
dig: dug
do: did done
draw: drew drawn
dream: dreamt
drink: drank drunk
drive: drove driven
eat: ate eaten
fall: fell fallen
feed: fed
feel: felt
fight: fought
find: found
flee: fled
fling: flung
fly: flew flown
forbid: forbade forbidden
forget: forgot forgotten
forgive: forgave forgiven
freeze: froze frozen
get: got gotten
give: gave given
go: went gone
grind: ground
grow: grew grown
hang: hung
have: had
hear: heard
hide: hid hidden
hold: held
keep: kept
kneel: knelt
know: knew known
lay: laid
lead: led
leap: leapt
leave: left
lend: lent
lie: lay lain
light: lit
lose: lost
make: made
mean: meant
mislead: misled
overcome: overcame
pay: paid
ride: rode ridden
ring: rang rung
rise: rose risen
run: ran
say: said
see: saw seen
seek: sought
sell: sold
send: sent
shake: shook shaken
shine: shone
shoot: shot
show: shown
shrink: shrank shrunk
sing: sang sung
sink: sank sunk
sit: sat
sleep: slept
slide: slid
speak: spoke spoken
spend: spent
spin: spun
spit: spat
spring: sprang sprung
stand: stood
steal: stole stolen
stick: stuck
sting: stung
stink: stank stunk
strike: struck
swear: swore sworn
sweep: swept
swim: swam swum
swing: swung
take: took taken
teach: taught
tear: tore torn
tell: told
think: thought
throw: threw thrown
tread: trod trodden
undergo: underwent undergone
understand: understood
undo: undid undone
wake: woke woken
wear: wore worn
weep: wept
win: won
wind: wound
withdraw: withdrew withdrawn
withstand: withstood
write: wrote written
arise: arose arisen
awake: awoke awoken
bear: bore borne
child: children
man: men
woman: women
foot: feet
tooth: teeth
mouse: mice
goose: geese
life: lives
knife: knives
wife: wives
wolf: wolves
half: halves
shelf: shelves
thief: thieves
loaf: loaves
good: better best
bad: worse worst
"""

IRREGULAR_FORMS = {}
for _line in _IRREGULAR_SPEC.strip().splitlines():
    _base, _, _forms = _line.partition(":")
    for _form in _forms.split():
        if _form != _base.strip():
            IRREGULAR_FORMS.setdefault(_form, _base.strip())

IRREGULAR_AMBIGUOUS = {
    'bent', 'best', 'better', 'bit', 'bore', 'bound', 'fell', 'felt', 'found',
    'ground', 'lay', 'left', 'lit', 'lives', 'men', 'rose', 'saw', 'stuck',
    'worse', 'worst', 'wound',
}


def normalize(word):
    return str(word or "").strip().lower().replace("’", "'")


@lru_cache(maxsize=1)
def _frequencies():
    """Read once on first use; missing/corrupt optional data never breaks lookup."""
    values = {}
    try:
        with _DATA_PATH.open(encoding="utf-8") as stream:
            for rank, line in enumerate(stream, start=1):
                token, count = line.rstrip().rsplit(" ", 1)
                if _WORD.fullmatch(token):
                    values[token] = (rank, int(count))
    except (OSError, ValueError):
        return {}
    return values


def base_forms(word):
    """Return locally known base forms, without discarding ambiguous senses.

    These are candidate forms, not assertions about the word's current sense:
    'saw' may be the noun or the past tense of 'see'. Callers should preserve
    their existing direct-entry-first handling for IRREGULAR_AMBIGUOUS.
    """
    token = normalize(word)
    values = _frequencies()
    result = []
    if token in IRREGULAR_FORMS:
        result.append(IRREGULAR_FORMS[token])
    if "'" in token:
        stem = token.split("'", 1)[0]
        if stem in values:
            result.append(stem)
    for suffix, replacement in (("ies", "y"), ("es", ""), ("s", ""),
                                ("ing", ""), ("ed", ""), ("ing", "e"), ("ed", "e")):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            stem = token[:-len(suffix)] + replacement
            for candidate in (stem, stem[:-1] if len(stem) > 3 and stem[-1] == stem[-2] else ""):
                if candidate in values and candidate != token:
                    result.append(candidate)
    return list(dict.fromkeys(result))


def lookup(word):
    """Frequency metadata suitable for a card; no translation or CEFR claim."""
    token = normalize(word)
    found = _frequencies().get(token)
    result = {"base_forms": base_forms(token)} if token in IRREGULAR_FORMS else {}
    if found:
        rank, count = found
        label = ("Дуже часте" if rank <= 1000 else "Часте" if rank <= 5000
                 else "Поширене" if rank <= 15000 else "Рідше")
        result.update(frequency_rank=rank, frequency_count=count,
                      frequency_label=label, base_forms=base_forms(token))
    return result


def _restore_case(candidate, original):
    if original.isupper():
        return candidate.upper()
    return candidate.capitalize() if original[:1].isupper() else candidate


def _one_edit(token):
    """Generate one-edit spellings; bounded to small OCR word tokens."""
    splits = [(token[:i], token[i:]) for i in range(len(token) + 1)]
    for left, right in splits:
        if right:
            yield left + right[1:]
            for letter in string.ascii_lowercase:
                yield left + letter + right[1:]
        if len(right) > 1:
            yield left + right[1] + right[0] + right[2:]
        for letter in string.ascii_lowercase:
            yield left + letter + right


def correct_ocr(word):
    """Suggest a strong local correction, or None when known/ambiguous.

    A frequent unambiguous one-edit candidate can correct unknown OCR tokens.
    Existing corpus words are preserved except a low-frequency dropped final
    'g' (e.g. 'changin'), when the complete '-ing' form is at least ten times
    more frequent. Manual entry can bypass this OCR-only function.
    """
    original = str(word or "")
    token = normalize(original)
    if not 5 <= len(token) <= 32 or not re.fullmatch(r"[a-z0-9]+", token):
        return None
    values = _frequencies()
    current_count = values.get(token, (0, 0))[1]
    if token.endswith("in"):
        candidate = token + "g"
        count = values.get(candidate, (0, 0))[1]
        if count >= max(1000, 10 * current_count):
            return _restore_case(candidate, original)
    if token in values:
        return None
    candidates = set(_one_edit(token))
    # OCR can fuse rn into m, or turn a letter into a numeral, more than once.
    for source, target in (("rn", "m"), ("0", "o"), ("1", "l"), ("1", "i"),
                           ("5", "s"), ("8", "b")):
        if source in token:
            candidates.add(token.replace(source, target))
    ranked = sorted(((values[candidate][1], candidate) for candidate in candidates
                     if candidate in values and values[candidate][0] <= 20000), reverse=True)
    if not ranked:
        return None
    count, candidate = ranked[0]
    if count < 1000 or (len(ranked) > 1 and count < 8 * ranked[1][0]):
        return None
    return _restore_case(candidate, original)

