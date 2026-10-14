"""Synthetic exam pages with known ground truth.

``generate_pages`` renders a seeded quiz (header, numbered questions with four
choices, a printed answer key in a footer box or on a trailing key page) to
greyscale PNG pages and returns each page with its ``PageTruth``. Everything
derives from ``random.Random(seed)`` so a suite can pin the bytes.

Renderers:

- ``clean``: stdlib only, the 8 x 13 bitmap font in ``_font.py`` scaled 2x at
  96 dpi, encoded through ``encode_png_gray``. Byte-deterministic for a seed on
  every platform.
- ``scan``: the ``clean`` bitmap with Pillow effects (light-grey paper, +-1.5
  degree rotation, blur, uniform-sum noise of sigma ~6, JPEG quality 70,
  back to PNG at 16 grey levels). ``dense``: Pillow's embedded default font at a 10.5 pt
  equivalent, two columns, 24-30 questions. Both need the optional ``bench``
  extra and are not byte-portable across Pillow builds; the manifest records
  ``pillow_version`` and ``platform``.

Ground truth is what the page PRINTS: ``Question.answer_key`` is the printed
key, and ``Page.printed_key_wrong`` lists the question numbers whose printed
key deliberately disagrees with the answer ``solve_question`` derives from the
stem and choices (the digitizer's "disagreements"). Every question family is
closed and rule-solvable so an in-process oracle can re-derive answers.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import random
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from architect.floors import EvaluationCase
from architect.models.images import MediaType, encode_png_gray

from ._font import GLYPH_HEIGHT, GLYPH_WIDTH, WORDS, draw_text
from .rows import BenchError

__all__ = [
    "Choice",
    "DatasetFiles",
    "EXAM_OUTPUT_SCHEMA",
    "GENERATOR_VERSION",
    "Label",
    "Page",
    "PageTruth",
    "Question",
    "STAGE1_OUTPUT_SCHEMA",
    "Style",
    "VISION_INPUT_SCHEMA",
    "cases_for",
    "digitizer_case",
    "digitizer_expected",
    "generate_pages",
    "manifest",
    "manifest_sha256",
    "page_image",
    "solve_question",
    "stage1_output",
    "vision_case",
    "write_dataset",
]

GENERATOR_VERSION = "v1"
Style = Literal["clean", "scan", "dense"]
Label = Literal["A", "B", "C", "D"]
LABELS: tuple[Label, ...] = ("A", "B", "C", "D")
LAYOUT_SOURCE = "rendered-synthetic-page"
# Pillow's embedded font at 14 px is a 10.5 pt equivalent at 96 dpi.
_DENSE_FONT_PX, _DENSE_LINE_PX, _DENSE_CHAR_PX = 14, 17, 7


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Choice(_Frozen):
    label: Label
    text: str


class Question(_Frozen):
    number: int
    stem: str
    choices: list[Choice]
    answer_key: Label


class PageTruth(_Frozen):
    page: int
    questions: list[Question]  # [] on a key page
    answer_key_page: bool


class Page(_Frozen):
    image: bytes
    media_type: MediaType
    width: int
    height: int
    truth: PageTruth
    sha256: str
    style: Style
    index: int
    printed_key_wrong: tuple[int, ...]  # question numbers whose printed key is deliberately wrong


class DatasetFiles(_Frozen):
    cases: Path
    manifest: Path
    pngs: tuple[Path, ...]


_LABEL_SCHEMA: JsonValue = {"enum": list(LABELS)}
_QUESTION_SCHEMA: JsonValue = {
    "type": "object",
    "required": ["number", "stem", "choices", "answer_key"],
    "properties": {
        "number": {"type": "integer"},
        "stem": {"type": "string"},
        "choices": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label", "text"],
                "properties": {"label": _LABEL_SCHEMA, "text": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        "answer_key": _LABEL_SCHEMA,
    },
    "additionalProperties": False,
}
EXAM_OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["page", "questions", "answer_key_page"],
    "properties": {
        "page": {"type": "integer"},
        "questions": {"type": "array", "items": _QUESTION_SCHEMA},
        "answer_key_page": {"type": "boolean"},
    },
    "additionalProperties": False,
}
STAGE1_OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["questions", "layout_source"],
    "properties": {
        "questions": {"type": "array", "items": _QUESTION_SCHEMA},
        "layout_source": {"const": LAYOUT_SOURCE},
    },
    "additionalProperties": False,
}
VISION_INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["synthetic", "page", "images"],
    "properties": {
        "synthetic": {"const": True},
        "page": {"type": "integer"},
        "images": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["$image"],
                "properties": {
                    "$image": {
                        "type": "object",
                        "required": ["media_type"],
                        "properties": {"media_type": {"type": "string"}},
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

# --- closed question families -------------------------------------------------

_UNITS: dict[tuple[str, str], int] = {
    ("centimeters", "meters"): 100,
    ("meters", "kilometers"): 1000,
    ("minutes", "hours"): 60,
    ("seconds", "minutes"): 60,
    ("grams", "kilograms"): 1000,
    ("hours", "days"): 24,
    ("inches", "feet"): 12,
    ("months", "years"): 12,
}
_NAMES = ("Mara", "Theo", "Priya", "Jonas", "Lena", "Omar")
_PLACES = ("market", "harbor", "garden", "village", "bakery", "library", "station")
_ITEMS = ("apples", "a lantern", "some rope", "a kite", "two lemons", "a basket", "a ticket")
_ANIMALS = frozenset(
    "rabbit dolphin eagle falcon kitten lizard otter spider beetle goose lobster parrot raven "
    "turtle wolf zebra donkey monkey owl robin tiger elephant frog giraffe horse koala narwhal "
    "penguin snail whale badger bee crab deer fox mouse eel".split()
)
_OBJECTS = frozenset(
    "candle ladder mirror needle pencil saddle table wagon anchor basket button carpet hammer "
    "jacket kettle lantern napkin parcel ribbon shovel ticket umbrella zipper barrel pillow quilt "
    "rocket violin whistle blanket guitar helmet kayak ruler trumpet flute drum bottle envelope "
    "jar kite lamp bucket glove hat key rope wheel vase tent ring sponge flag log oar van".split()
)
_PLACE_WORDS = frozenset(
    "river garden forest island market valley desert meadow harbor jungle ocean tunnel canyon "
    "mountain village lake hill cabin glacier iceberg nest kennel gate fence hedge bridge".split()
)
_FOODS = frozenset(
    "apple orange lemon honey jelly noodle onion peach cherry grape yogurt tomato coconut mango "
    "olive pumpkin walnut egg jam pear nut".split()
)
_CATEGORIES: dict[str, frozenset[str]] = {
    "animal": _ANIMALS,
    "object": _OBJECTS,
    "place": _PLACE_WORDS,
    "food": _FOODS,
}
# (sentence with ___, answer word, answer category); distractors come from other categories.
_SENTENCES: tuple[tuple[str, str, str], ...] = (
    ("The ___ hopped across the field.", "rabbit", "animal"),
    ("A ___ swam beside the boat.", "dolphin", "animal"),
    ("The ___ hooted from the barn at night.", "owl", "animal"),
    ("A small ___ crawled up the wall.", "spider", "animal"),
    ("The ___ barked at the mail carrier.", "fox", "animal"),
    ("A striped ___ grazed with its herd.", "zebra", "animal"),
    ("The ___ sang in the tree every morning.", "robin", "animal"),
    ("A ___ carried its shell on its back.", "snail", "animal"),
    ("She lit a ___ when the power went out.", "candle", "object"),
    ("He climbed the ___ to reach the roof.", "ladder", "object"),
    ("Write your name with a ___.", "pencil", "object"),
    ("The rain stopped, so I closed my ___.", "umbrella", "object"),
    ("She played a tune on her ___.", "violin", "object"),
    ("He wore a ___ to keep his head safe.", "helmet", "object"),
    ("Measure the line with a ___.", "ruler", "object"),
    ("Please fill the ___ with water.", "bucket", "object"),
    ("Put the letter in an ___ and seal it.", "envelope", "object"),
    ("We paddled the ___ across the pond.", "kayak", "object"),
    ("Fish swim in the ___ near our town.", "river", "place"),
    ("Tall trees filled the dark ___.", "forest", "place"),
    ("The boats returned to the ___ at dusk.", "harbor", "place"),
    ("Camels crossed the hot ___.", "desert", "place"),
    ("The train went through a long ___.", "tunnel", "place"),
    ("Climbers reached the peak of the ___.", "mountain", "place"),
    ("We crossed the ___ over the stream.", "bridge", "place"),
    ("The bird laid three eggs in its ___.", "nest", "place"),
    ("She peeled an ___ for lunch.", "orange", "food"),
    ("The bees made sweet ___.", "honey", "food"),
    ("Spread the ___ on the toast.", "jam", "food"),
    ("A ripe ___ fell from the tree.", "peach", "food"),
    ("He cracked an ___ into the pan.", "egg", "food"),
    ("The soup needs one more chopped ___.", "onion", "food"),
    ("We scooped out the ___ and carved a face.", "pumpkin", "food"),
    ("Slice the ___ for the salad.", "tomato", "food"),
)
# Words that could plausibly complete a sentence of another category (creatures and people
# outside the animal set, water and ground words, trees and climbing plants, carvable and
# water-holding materials, writing tools, voices); never drawn as distractors.
_NEVER_DISTRACTORS = frozenset(
    "insect quokka unicorn dragon oyster knight sailor queen wizard nephew uncle acorn "
    "yacht wave sand ice quill feather ink vessel urn goblet tree oak willow vine rose "
    "leaf cactus stone marble voice window".split()
)
# Category words that fit one particular sentence of another category; excluded from that
# sentence's distractors only. Together with the category rule and _NEVER_DISTRACTORS this
# gives every sentence question exactly one sensible completion for a human or model reader.
_SENTENCE_EXCLUDE: dict[str, frozenset[str]] = {
    "The ___ hooted from the barn at night.": frozenset({"whistle", "trumpet", "flute", "engine"}),
    "Write your name with a ___.": frozenset({"nail"}),
    "He climbed the ___ to reach the roof.": frozenset({"fence", "gate", "hedge", "hill"}),
    "Please fill the ___ with water.": frozenset({"lake", "canyon", "valley", "ocean"}),
    "Measure the line with a ___.": frozenset({"yarn"}),
    "We crossed the ___ over the stream.": frozenset({"log", "rope", "ladder"}),
    "The bird laid three eggs in its ___.": frozenset({"basket", "hat", "carpet", "blanket"}),
    "She peeled an ___ for lunch.": frozenset({"eel", "envelope"}),
    "The soup needs one more chopped ___.": frozenset({"crab", "lobster", "eel", "daisy"}),
    "We scooped out the ___ and carved a face.": frozenset({"log", "barrel"}),
    "Slice the ___ for the salad.": frozenset({"crab", "lobster", "eel", "daisy"}),
}
_SENTENCE_ANSWERS: dict[str, str] = {sentence: word for sentence, word, _ in _SENTENCES}
if not all(word in WORDS for _, word, _ in _SENTENCES):  # pragma: no cover - data guard
    raise RuntimeError("Every sentence answer must be in WORDS")
if any(word not in _CATEGORIES[category] for _, word, category in _SENTENCES):  # pragma: no cover
    raise RuntimeError("Every sentence answer must belong to its category")
if not set(_SENTENCE_EXCLUDE) <= set(_SENTENCE_ANSWERS):  # pragma: no cover - data guard
    raise RuntimeError("Every sentence exclusion must name a sentence")


def _sentence_pool(sentence: str, category: str) -> list[str]:
    """Distractors for one sentence: other categories and uncategorised words, minus words
    that could also complete it."""
    excluded = _CATEGORIES[category] | _NEVER_DISTRACTORS | _SENTENCE_EXCLUDE.get(sentence, set())
    return sorted(word for word in WORDS if word not in excluded)

_ARITH = re.compile(r"^What is (\d+) ([+x-]) (\d+)\?$")
_UNIT = re.compile(r"^How many (\w+) are in (\d+) (\w+)\?$")
_WORD = re.compile(r"^Which word completes the sentence: (.+)$", re.DOTALL)
_EXTREME = re.compile(r"^Which number is the (largest|smallest)\?$")
_PASSAGE = re.compile(
    r"^(\w+) walked to the (\w+) to buy (.+?)\. \w+ paid with (\d+) coins\.\n(.+)$", re.DOTALL
)


@dataclass
class _Draft:
    stem: str
    choices: list[str]
    correct: int  # index into choices
    family: str


def _distinct(rng: random.Random, correct: int, near: Sequence[int]) -> list[int]:
    values = [correct]
    for candidate in near:
        if candidate not in values and candidate >= 0:
            values.append(candidate)
    while len(values) < 4:
        candidate = correct + rng.randint(-20, 20)
        if candidate not in values and candidate >= 0:
            values.append(candidate)
    return values[:4]


def _arithmetic(rng: random.Random) -> _Draft:
    op = rng.choice("+-x")
    if op == "+":
        a, b = rng.randint(11, 89), rng.randint(11, 89)
        value = a + b
    elif op == "-":
        a, b = rng.randint(30, 99), rng.randint(2, 29)
        value = a - b
    else:
        a, b = rng.randint(3, 12), rng.randint(3, 12)
        value = a * b
    near = [value + 1, value - 1, value + 10, value - 10, int(str(value)[::-1] or 0)]
    rng.shuffle(near)
    values = _distinct(rng, value, near)
    rng.shuffle(values)
    return _Draft(f"What is {a} {op} {b}?", [str(v) for v in values], values.index(value), "arith")


def _unit(rng: random.Random) -> _Draft:
    (small, big), factor = rng.choice(list(_UNITS.items()))
    amount = rng.randint(2, 9)
    value = amount * factor
    near = [value // 10, value * 10, value + factor, value - factor, amount + factor]
    values = _distinct(rng, value, near)
    rng.shuffle(values)
    stem = f"How many {small} are in {amount} {big}?"
    return _Draft(stem, [str(v) for v in values], values.index(value), "unit")


def _word(rng: random.Random) -> _Draft:
    sentence, answer, category = rng.choice(_SENTENCES)
    distractors = rng.sample(_sentence_pool(sentence, category), 3)
    words = [answer, *distractors]
    rng.shuffle(words)
    return _Draft(
        f"Which word completes the sentence: {sentence}", words, words.index(answer), "word"
    )


def _extreme(rng: random.Random) -> _Draft:
    which = rng.choice(("largest", "smallest"))
    decimals = rng.random() < 0.3
    values: list[float] = []
    while len(values) < 4:
        value = round(rng.uniform(1, 999), 1) if decimals else float(rng.randint(10, 9999))
        if value not in values:
            values.append(value)
    target = max(values) if which == "largest" else min(values)
    texts = [f"{v:.1f}" if decimals else str(int(v)) for v in values]
    return _Draft(f"Which number is the {which}?", texts, values.index(target), "extreme")


def _passage(rng: random.Random) -> _Draft:
    name = rng.choice(_NAMES)
    place, item, coins = rng.choice(_PLACES), rng.choice(_ITEMS), rng.randint(2, 9)
    pronoun = "She" if name in {"Mara", "Priya", "Lena"} else "He"
    passage = f"{name} walked to the {place} to buy {item}. {pronoun} paid with {coins} coins."
    kind = rng.choice(("place", "item", "coins"))
    if kind == "place":
        question = f"Where did {name} go?"
        answer, pool = place, [p for p in _PLACES if p != place]
    elif kind == "item":
        question = f"What did {name} buy?"
        answer, pool = item, [i for i in _ITEMS if i != item]
    else:
        question = f"How many coins did {name} pay?"
        answer, pool = str(coins), [str(c) for c in range(2, 10) if c != coins]
    choices = [answer, *rng.sample(pool, 3)]
    rng.shuffle(choices)
    return _Draft(f"{passage}\n{question}", choices, choices.index(answer), "passage")


_FAMILIES = (_arithmetic, _unit, _word, _extreme, _passage)


def solve_question(question: Question) -> Label:
    """Re-derive the correct label from the stem and choices by closed rules."""
    stem = question.stem.strip()
    texts = [choice.text for choice in question.choices]

    def label_of(text: str) -> Label:
        for choice in question.choices:
            if choice.text == text:
                return choice.label
        raise ValueError("unsolvable_question")

    if match := _ARITH.match(stem):
        a, op, b = int(match[1]), match[2], int(match[3])
        value = a + b if op == "+" else a - b if op == "-" else a * b
        return label_of(str(value))
    if match := _UNIT.match(stem):
        factor = _UNITS.get((match[1], match[3]))
        if factor is None:
            raise ValueError("unsolvable_question")
        return label_of(str(int(match[2]) * factor))
    if match := _WORD.match(stem):
        answer = _SENTENCE_ANSWERS.get(match[1].strip())
        if answer is None:
            raise ValueError("unsolvable_question")
        return label_of(answer)
    if match := _EXTREME.match(stem):
        try:
            values = [float(text) for text in texts]
        except ValueError as exc:
            raise ValueError("unsolvable_question") from exc
        target = max(values) if match[1] == "largest" else min(values)
        return label_of(texts[values.index(target)])
    if match := _PASSAGE.match(stem):
        name, place, item, coins, ask = match.groups()
        if ask == f"Where did {name} go?":
            return label_of(place)
        if ask == f"What did {name} buy?":
            return label_of(item)
        if ask == f"How many coins did {name} pay?":
            return label_of(coins)
    raise ValueError("unsolvable_question")


# --- layout -------------------------------------------------------------------


@dataclass
class _Layout:
    columns: int
    cols: int  # characters per column line
    rows: int  # lines per column (question area)
    header: list[str] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)  # full-width lines under a rule
    body: list[tuple[int, int, str]] = field(default_factory=list)  # (column, row, text)


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if len(candidate) <= width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _pack(tokens: Sequence[str], width: int, separator: str = "  ") -> list[str]:
    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = token if not current else f"{current}{separator}{token}"
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = token
    if current:
        lines.append(current)
    return lines


def _question_lines(
    number: int, draft: _Draft, cols: int, numbering: str, label_style: str
) -> list[str]:
    prefix = {"1.": f"{number}. ", "1)": f"{number}) ", "Q1": f"Q{number} "}[numbering]
    indent = " " * len(prefix)
    width = cols - len(prefix)
    stem_lines = _wrap(draft.stem, width)
    labels = [
        {"A.": f"{label}.", "(A)": f"({label})", "A)": f"{label})"}[label_style] for label in LABELS
    ]
    tokens = [f"{label} {text}" for label, text in zip(labels, draft.choices, strict=True)]
    return [prefix + stem_lines[0]] + [indent + line for line in stem_lines[1:]] + [
        indent + line for line in _pack(tokens, width)
    ]


@dataclass
class _Quiz:
    unit: int
    numbering: str
    label_style: str
    key_page: bool
    pages: list[list[Question]]
    wrong: list[tuple[int, ...]]  # per question page


# Question-count range per style (Section 5.1); a page never holds fewer than the minimum.
_QUESTION_RANGE: dict[str, tuple[int, int]] = {"dense": (24, 30), "clean": (8, 12), "scan": (8, 12)}
# Families whose questions render as one stem line and one choice line at the suite geometry;
# the planner reserves room for them so a page can always reach its minimum count.
_SHORT_FAMILIES = (_arithmetic, _unit, _extreme)
_SHORT_LINES = 2
_SHORT_ATTEMPTS = 32
# An arithmetic question ("12) What is 89 + 89?" / "(A) 178  (B) 177  (C) 188  (D) 871")
# renders in two lines once a column holds this many characters.
_MIN_COLUMN_CHARS = 40


def _position(
    column: int, row: int, height: int, columns: int, rows: int
) -> tuple[int, int] | None:
    """Where a ``height``-line question starts from (column, row), or None when it cannot fit."""
    if row + height > rows and column + 1 < columns:
        column, row = column + 1, 0
    if row + height > rows:
        return None
    return column, row


def _capacity(column: int, row: int, columns: int, rows: int) -> int:
    """How many short questions still fit after (column, row)."""
    return (rows - row) // _SHORT_LINES + (columns - column - 1) * (rows // _SHORT_LINES)


@dataclass
class _PageFill:
    """One question page being filled top to bottom, column by column."""

    quiz: _Quiz
    layout: _Layout
    minimum: int
    first_number: int
    questions: list[Question] = field(default_factory=list)
    used: dict[str, int] = field(default_factory=lambda: {"passage": 0, "word": 0})
    column: int = 0
    row: int = 0

    def place(self, draft: _Draft) -> bool:
        """Place ``draft`` when it fits and still leaves room for the page minimum."""
        layout = self.layout
        number = self.first_number + len(self.questions) + 1
        lines = _question_lines(
            number, draft, layout.cols, self.quiz.numbering, self.quiz.label_style
        )
        start = _position(self.column, self.row, len(lines), layout.columns, layout.rows)
        if start is None:
            return False
        still_needed = max(0, self.minimum - len(self.questions) - 1)
        end = start[1] + len(lines)
        if _capacity(start[0], end, layout.columns, layout.rows) < still_needed:
            return False
        self.column, self.row = start[0], end
        for offset, line in enumerate(lines):
            layout.body.append((start[0], start[1] + offset, line))
        if draft.family in self.used:
            self.used[draft.family] += 1
        pairs = zip(LABELS, draft.choices, strict=True)
        choices = [Choice(label=label, text=text) for label, text in pairs]
        key = LABELS[draft.correct]
        self.questions.append(
            Question(number=number, stem=draft.stem, choices=choices, answer_key=key)
        )
        return True


def _plan(
    rng: random.Random,
    count: int,
    style: Style,
    columns: int,
    cols: int,
    rows: int,
    key_page: bool | None,
) -> tuple[_Quiz, list[_Layout]]:
    unit = rng.randint(1, 12)
    use_key_page = rng.random() < 0.5 if key_page is None else key_page
    numbering = rng.choice(("1.", "1)", "Q1"))
    label_style = rng.choice(("A.", "(A)", "A)"))
    quiz = _Quiz(unit, numbering, label_style, use_key_page, [], [])
    layouts: list[_Layout] = []
    number = 0
    minimum, maximum = _QUESTION_RANGE[style]
    for _page in range(count):
        target = rng.randint(minimum, maximum)
        caps = {"passage": max(1, target // 4), "word": max(1, target // 5)}
        layout = _Layout(columns, cols, rows)
        fill = _PageFill(quiz, layout, minimum, number)
        for _slot in range(target):
            placed = False
            for _attempt in range(6):
                draft = rng.choice(_FAMILIES)(rng)
                if draft.family in caps and fill.used[draft.family] >= caps[draft.family]:
                    continue
                if fill.place(draft):
                    placed = True
                    break
            if placed:
                continue
            if len(fill.questions) >= minimum:
                break
            # Below the minimum: fall back to the short families the planner reserved room for.
            for _attempt in range(_SHORT_ATTEMPTS):
                if fill.place(rng.choice(_SHORT_FAMILIES)(rng)):
                    break
            else:
                raise ValueError("page geometry cannot hold the minimum question count")
        questions = fill.questions
        number += len(questions)
        wrong: tuple[int, ...] = ()
        if questions and rng.random() < 0.2:
            picks = sorted(rng.sample([q.number for q in questions], min(2, len(questions))))
            wrong = tuple(picks[: rng.randint(1, len(picks))])
        # The printed key replaces the derived key on the deliberately wrong questions.
        printed: list[Question] = []
        for question in questions:
            if question.number in wrong:
                others = [label for label in LABELS if label != question.answer_key]
                question = question.model_copy(update={"answer_key": rng.choice(others)})
            printed.append(question)
        quiz.pages.append(printed)
        quiz.wrong.append(wrong)
        layouts.append(layout)
    return quiz, layouts


def _key_tokens(questions: Sequence[Question]) -> list[str]:
    return [f"{q.number}-{q.answer_key}" for q in questions]


def _finish(quiz: _Quiz, layouts: list[_Layout], cols: int, columns: int, rows: int) -> None:
    total = len(quiz.pages) + (1 if quiz.key_page else 0)
    for index, (layout, questions) in enumerate(zip(layouts, quiz.pages, strict=True)):
        title = f"Unit {quiz.unit} Quiz"
        page_label = f"Page {index + 1} of {total}"
        width = cols * columns + (columns - 1) * 2
        layout.header = [
            title + " " * max(1, width - len(title) - len(page_label)) + page_label,
            "Name: ____________    Date: ____________",
            "",
        ]
        if not quiz.key_page:
            layout.footer = _wrap("Answer Key: " + " ".join(_key_tokens(questions)), width)
    if quiz.key_page:
        layout = _Layout(columns, cols, rows)
        title = f"Unit {quiz.unit} Quiz - Answer Key"
        page_label = f"Page {total} of {total}"
        width = cols * columns + (columns - 1) * 2
        layout.header = [
            title + " " * max(1, width - len(title) - len(page_label)) + page_label,
            "",
            "",
        ]
        lines = _key_lines(quiz, cols)
        if len(lines) > rows * columns:  # guarded by _layouts; never print a partial key
            raise ValueError("answer key does not fit on one key page")
        for index, line in enumerate(lines):
            layout.body.append((index // rows, index % rows, line))
        layouts.append(layout)


def _key_lines(quiz: _Quiz, cols: int) -> list[str]:
    """The key page's printed lines: every question's printed key, in order."""
    tokens = [token for questions in quiz.pages for token in _key_tokens(questions)]
    return _pack(tokens, cols, "   ")


def _layouts(
    seed: int, count: int, style: Style, dpi: int, key_page: bool | None
) -> tuple[random.Random, _Quiz, list[_Layout]]:
    """Plan and lay out a quiz; the returned rng continues into the ``scan`` renderer.

    A trailing key page holds every printed key (continuing into a second column on
    two-column styles). When the key cannot fit on one page, a seed-chosen key page
    falls back to per-page footer keys and an explicit ``key_page=True`` is refused.
    """
    rng = random.Random(seed)
    _width, _height, _scale, _margin, columns, cols, rows = _geometry(style, dpi)
    if cols < _MIN_COLUMN_CHARS or columns * (rows // _SHORT_LINES) < _QUESTION_RANGE[style][0]:
        raise ValueError(f"dpi {dpi} is too small for the {style} layout")
    quiz, layouts = _plan(rng, count, style, columns, cols, rows, key_page)
    if quiz.key_page and len(_key_lines(quiz, cols)) > rows * columns:
        if key_page:
            raise ValueError("answer key does not fit on one key page; use key_page=False")
        quiz.key_page = False
    _finish(quiz, layouts, cols, columns, rows)
    return rng, quiz, layouts


# --- renderers ----------------------------------------------------------------


def _bitmap(layout: _Layout, width: int, height: int, scale: int, margin: int) -> list[bytearray]:
    canvas = [bytearray(b"\xff" * width) for _ in range(height)]
    char_w, line_h = GLYPH_WIDTH * scale, GLYPH_HEIGHT * scale
    column_px = layout.cols * char_w
    gutter = 2 * char_w
    for row, line in enumerate(layout.header):
        draw_text(canvas, margin, margin + row * line_h, line, scale=scale)
    top = margin + len(layout.header) * line_h
    for column, row, line in layout.body:
        x = margin + column * (column_px + gutter)
        draw_text(canvas, x, top + row * line_h, line, scale=scale)
    if layout.footer:
        footer_top = height - margin - len(layout.footer) * line_h
        rule_y = footer_top - line_h // 2
        for x in range(margin, width - margin):
            canvas[rule_y][x] = 0
            canvas[rule_y + 1][x] = 0
        for row, line in enumerate(layout.footer):
            draw_text(canvas, margin, footer_top + row * line_h, line, scale=scale)
    return canvas


def _pillow() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType, ModuleType, str]:
    try:
        image = importlib.import_module("PIL.Image")
        draw = importlib.import_module("PIL.ImageDraw")
        font = importlib.import_module("PIL.ImageFont")
        filters = importlib.import_module("PIL.ImageFilter")
        chops = importlib.import_module("PIL.ImageChops")
        version = str(importlib.import_module("PIL").__version__)
    except ImportError as exc:
        raise BenchError("bench_extra_missing") from exc
    return image, draw, font, filters, chops, version


def _png_bytes(page: object) -> bytes:
    buffer = io.BytesIO()
    save = getattr(page, "save")  # noqa: B009 - Pillow object from a dynamic import
    save(buffer, format="PNG")
    return buffer.getvalue()


def _scan(rows: Sequence[bytearray], width: int, height: int, rng: random.Random) -> bytes:
    image, _draw, _font, filters, chops, _version = _pillow()
    page = image.frombytes("L", (width, height), b"".join(bytes(row) for row in rows))
    page = page.point(lambda value: 235 if value > 235 else value)
    page = page.rotate(rng.uniform(-1.5, 1.5), resample=image.Resampling.BICUBIC, fillcolor=235)
    page = page.filter(filters.GaussianBlur(0.6))
    # Sum of two uniform(0..14) draws centred on zero: sigma ~ 6.1 without per-pixel Python.
    noise_a = image.frombytes("L", (width, height), rng.randbytes(width * height))
    noise_b = image.frombytes("L", (width, height), rng.randbytes(width * height))
    lut = [value % 15 for value in range(256)]
    combined = chops.add(noise_a.point(lut), noise_b.point(lut))
    page = chops.add(page, combined, 1.0, -14)
    jpeg = io.BytesIO()
    page.save(jpeg, format="JPEG", quality=70)
    jpeg.seek(0)
    # 16 grey levels after the JPEG round trip keep the scanned look while the PNG stays
    # near 150 KB instead of 500 KB of incompressible noise.
    return _png_bytes(image.open(jpeg).convert("L").point(lambda value: value & 0xF0))


def _dense_render(layout: _Layout, width: int, height: int, margin: int) -> bytes:
    image, draw_mod, font_mod, _filters, _chops, _version = _pillow()
    font = font_mod.load_default(size=_DENSE_FONT_PX)
    page = image.new("L", (width, height), 255)
    draw = draw_mod.Draw(page)
    line_h, char_w = _DENSE_LINE_PX, _DENSE_CHAR_PX
    column_px = layout.cols * char_w
    gutter = 2 * char_w
    for row, line in enumerate(layout.header):
        draw.text((margin, margin + row * line_h), line, font=font, fill=0)
    top = margin + len(layout.header) * line_h
    for column, row, line in layout.body:
        draw.text(
            (margin + column * (column_px + gutter), top + row * line_h), line, font=font, fill=0
        )
    if layout.footer:
        footer_top = height - margin - len(layout.footer) * line_h
        rule_y = footer_top - line_h // 2
        draw.line([(margin, rule_y), (width - margin, rule_y)], fill=0, width=2)
        for row, line in enumerate(layout.footer):
            draw.text((margin, footer_top + row * line_h), line, font=font, fill=0)
    return _png_bytes(page)


# --- public API ---------------------------------------------------------------


def _geometry(style: Style, dpi: int) -> tuple[int, int, int, int, int, int, int]:
    """(width, height, scale, margin, columns, cols, rows) for a style at ``dpi``."""
    width, height = round(8.5 * dpi), round(11 * dpi)
    margin = width // 17
    if style == "dense":
        columns = 2
        cols = (width - 2 * margin - 2 * _DENSE_CHAR_PX) // (columns * _DENSE_CHAR_PX)
        rows = (height - 2 * margin) // _DENSE_LINE_PX - 3 - 3
        return width, height, 1, margin, columns, cols, rows
    # Floor, not round: the page always keeps >= 45 characters per line and >= 30 lines,
    # the geometry the question families and the minimum question count need.
    scale = max(1, dpi // 48)
    char_w, line_h = GLYPH_WIDTH * scale, GLYPH_HEIGHT * scale
    columns = 1
    cols = (width - 2 * margin) // char_w
    rows = (height - 2 * margin) // line_h - 3 - 3
    return width, height, scale, margin, columns, cols, rows


def generate_pages(
    seed: int,
    count: int,
    style: Style = "clean",
    *,
    dpi: int = 96,
    key_page: bool | None = None,
) -> list[Page]:
    """Render ``count`` question pages (plus a key page when chosen) for ``seed``.

    Every question page holds 8-12 questions (``dense``: 24-30) and every printed key
    appears on some page. ``ValueError`` when ``dpi`` is too small for the style's
    layout (``dense`` needs ``dpi >= 77``) or when ``key_page=True`` but the key does
    not fit on one key page; a seed-chosen key page falls back to footer keys instead.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    if style not in ("clean", "scan", "dense"):
        raise ValueError("unknown style")
    if dpi < 48 or dpi > 300:
        raise ValueError("dpi must be between 48 and 300")
    width, height, scale, margin, _columns, _cols, _rows = _geometry(style, dpi)
    rng, quiz, layouts = _layouts(seed, count, style, dpi, key_page)
    pages: list[Page] = []
    all_wrong = tuple(sorted({number for wrong in quiz.wrong for number in wrong}))
    for index, layout in enumerate(layouts):
        is_key = quiz.key_page and index == len(layouts) - 1
        if style == "dense":
            data = _dense_render(layout, width, height, margin)
        else:
            canvas = _bitmap(layout, width, height, scale, margin)
            if style == "scan":
                data = _scan(canvas, width, height, rng)
            else:
                data = encode_png_gray([bytes(row) for row in canvas], width, height)
        truth = PageTruth(
            page=index + 1,
            questions=[] if is_key else list(quiz.pages[index]),
            answer_key_page=is_key,
        )
        pages.append(
            Page(
                image=data,
                media_type="image/png",
                width=width,
                height=height,
                truth=truth,
                sha256=hashlib.sha256(data).hexdigest(),
                style=style,
                index=index,
                printed_key_wrong=all_wrong if is_key else quiz.wrong[index],
            )
        )
    return pages


def page_image(page: Page) -> JsonValue:
    """The task-JSON image literal for a page."""
    import base64

    return {
        "$image": {
            "media_type": page.media_type,
            "base64": base64.b64encode(page.image).decode("ascii"),
        }
    }


def _metadata(style: Style) -> dict[str, JsonValue]:
    return {
        "synthetic": True,
        "source": LAYOUT_SOURCE,
        "style": style,
        "generator_version": GENERATOR_VERSION,
    }


def _task(pages: Sequence[Page]) -> JsonValue:
    return {
        "synthetic": True,
        "page": pages[0].truth.page,
        "images": [page_image(page) for page in pages],
    }


def vision_case(page: Page, *, case_id: str) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        task=_task([page]),
        expected=page.truth.model_dump(mode="json"),
        metadata=_metadata(page.style),
    )


def stage1_output(pages: Sequence[Page]) -> JsonValue:
    """What a faithful vision stage extracts: every printed question and key."""
    questions: list[JsonValue] = [
        q.model_dump(mode="json") for page in pages for q in page.truth.questions
    ]
    return {"questions": questions, "layout_source": LAYOUT_SOURCE}


def digitizer_expected(pages: Sequence[Page]) -> JsonValue:
    """Derived keys, printed disagreements, review always required."""
    questions = [q for page in pages for q in page.truth.questions]
    wrong = {number for page in pages for number in page.printed_key_wrong}
    return {
        "answer_keys": [{"id": f"q{q.number}", "answer": solve_question(q)} for q in questions],
        "disagreements": [f"q{q.number}" for q in questions if q.number in wrong],
        "review_required": True,
    }


def digitizer_case(pages: Sequence[Page], *, case_id: str) -> EvaluationCase:
    if not pages or not any(page.truth.questions for page in pages):
        raise ValueError("A digitizer case needs at least one question page")
    return EvaluationCase(
        case_id=case_id,
        task=_task(pages),
        expected=digitizer_expected(pages),
        metadata=_metadata(pages[0].style),
    )


def cases_for(
    pages: Sequence[Page], kind: Literal["vision", "digitizer"], *, dataset_id: str
) -> list[EvaluationCase]:
    """One case per page; key pages are vision cases only."""
    cases: list[EvaluationCase] = []
    for page in pages:
        case_id = f"{dataset_id}-{page.index:03d}"
        if kind == "vision":
            cases.append(vision_case(page, case_id=case_id))
        elif page.truth.questions:
            cases.append(digitizer_case([page], case_id=case_id))
    return cases


def manifest(
    pages: Sequence[Page], *, seed: int, dpi: int = 96, style: Style | None = None
) -> dict[str, JsonValue]:
    """Content-free description: seeds, sizes, sha256s, renderer provenance."""
    if not pages:
        raise ValueError("manifest needs at least one page")
    page_style = style or pages[0].style
    pillow_version: str | None = None
    if page_style != "clean":
        pillow_version = _pillow()[5]
    return {
        "seed": seed,
        "count": sum(1 for page in pages if not page.truth.answer_key_page),
        "style": page_style,
        "dpi": dpi,
        "generator_version": GENERATOR_VERSION,
        "pillow_version": pillow_version,
        "platform": sys.platform,
        "pages": [
            {
                "index": page.index,
                "sha256": page.sha256,
                "questions": len(page.truth.questions),
                "answer_key_page": page.truth.answer_key_page,
            }
            for page in pages
        ],
    }


def manifest_sha256(payload: dict[str, JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_dataset(
    directory: Path,
    pages: Sequence[Page],
    kind: Literal["vision", "digitizer"],
    *,
    dataset_id: str,
    seed: int = 0,
    dpi: int | None = None,
    png: bool = False,
) -> DatasetFiles:
    """Write ``cases.json`` and ``manifest.json``; PNG files only when ``png``.

    ``seed`` is recorded in the manifest (a ``Page`` does not carry it); ``dpi``
    defaults to the rendered page width over 8.5 inches.
    """
    if not pages:
        raise ValueError("write_dataset needs at least one page")
    if dpi is None:
        dpi = round(pages[0].width / 8.5)
    directory.mkdir(parents=True, exist_ok=True)
    cases = cases_for(pages, kind, dataset_id=dataset_id)
    cases_path = directory / "cases.json"
    cases_path.write_text(
        json.dumps([case.model_dump(mode="json") for case in cases], indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest(pages, seed=seed, dpi=dpi), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pngs: list[Path] = []
    if png:
        for page in pages:
            path = directory / f"page-{page.index:03d}.png"
            path.write_bytes(page.image)
            pngs.append(path)
    return DatasetFiles(cases=cases_path, manifest=manifest_path, pngs=tuple(pngs))
