"""Synthetic exam pages: pinned bytes, ground truth, layouts, datasets (Phase 8 WP3)."""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import struct
import zlib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from architect.bench import pages as pages_module
from architect.bench._font import GLYPH_HEIGHT, GLYPH_WIDTH, GLYPHS, WORDS, draw_text
from architect.bench.pages import (
    EXAM_OUTPUT_SCHEMA,
    STAGE1_OUTPUT_SCHEMA,
    VISION_INPUT_SCHEMA,
    Choice,
    Page,
    PageTruth,
    Question,
    cases_for,
    digitizer_case,
    digitizer_expected,
    generate_pages,
    manifest,
    manifest_sha256,
    solve_question,
    stage1_output,
    write_dataset,
)
from architect.bench.rows import BenchError
from architect.floors import EvaluationCase
from architect.models.images import ImageInput, sniff

# sha256 of each page of generate_pages(7, 2, "clean"): the stdlib renderer is
# byte-deterministic on every platform. Changing the font, layout or families
# changes these, and that is a generator version bump.
PINNED_SEED7_COUNT2 = (
    "a458f2a96842c35cc57dd515a49d3feee202d885576caae5772822aee355ad43",
    "fc74ff818ee9e715253aeba71a2e0126660bfcd1f537925720588cad4acffadc",
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _decode_gray(data: bytes) -> tuple[int, int, list[bytes]]:
    """Minimal decoder for the encoder's filter-0 greyscale output."""
    assert data.startswith(PNG_SIGNATURE)
    offset, idat, width, height = 8, b"", 0, 0
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            assert (depth, colour) == (8, 0)
        elif kind == b"IDAT":
            idat += body
        offset += 12 + length
    raw = zlib.decompress(idat)
    stride = width + 1
    rows = [raw[i * stride : (i + 1) * stride] for i in range(height)]
    assert all(row[0] == 0 for row in rows)
    return width, height, [row[1:] for row in rows]


def test_clean_pages_are_byte_pinned_for_seed_7() -> None:
    first = generate_pages(7, 2, "clean")
    second = generate_pages(7, 2, "clean")
    assert tuple(page.sha256 for page in first) == PINNED_SEED7_COUNT2
    assert [page.image for page in first] == [page.image for page in second]
    for page in first:
        assert hashlib.sha256(page.image).hexdigest() == page.sha256


def test_clean_page_is_a_valid_letter_png() -> None:
    page = generate_pages(7, 1, "clean", key_page=False)[0]
    assert page.image.startswith(PNG_SIGNATURE)
    assert (page.width, page.height) == (816, 1056)
    assert sniff(page.image) == ("image/png", 816, 1056)
    image = ImageInput.from_bytes(page.image)
    assert (image.width, image.height, image.sha256) == (816, 1056, page.sha256)
    width, height, rows = _decode_gray(page.image)
    assert (width, height) == (816, 1056)
    ink = sum(row.count(0) for row in rows)
    assert 0 < ink < width * height // 4  # printed text, mostly paper
    assert page.media_type == "image/png"
    assert page.style == "clean"
    assert page.index == 0
    assert page.truth.page == 1


def test_dpi_scales_the_page() -> None:
    page = generate_pages(3, 1, "clean", dpi=48, key_page=False)[0]
    assert (page.width, page.height) == (408, 528)
    assert sniff(page.image)[1:] == (408, 528)
    with pytest.raises(ValueError):
        generate_pages(3, 1, "clean", dpi=20)


def test_invalid_arguments_are_rejected() -> None:
    with pytest.raises(ValueError):
        generate_pages(1, 0)
    with pytest.raises(ValueError):
        generate_pages(1, 1, "blurry")  # type: ignore[arg-type]


def test_question_counts_and_numbering_for_clean_pages() -> None:
    for seed in range(12):
        rendered = generate_pages(seed, 3, "clean")
        numbers = [q.number for page in rendered for q in page.truth.questions]
        assert numbers == list(range(1, len(numbers) + 1))  # continuous across pages
        for page in rendered:
            if page.truth.answer_key_page:
                continue
            assert 8 <= len(page.truth.questions) <= 12
            for question in page.truth.questions:
                assert [choice.label for choice in question.choices] == ["A", "B", "C", "D"]
                assert len({choice.text for choice in question.choices}) == 4


_KEY_TOKEN = re.compile(r"\b(\d+)-([ABCD])\b")


def _printed_keys(layouts: list[pages_module._Layout]) -> dict[int, str]:
    """Every "<number>-<label>" token the layouts print (footers and key-page body)."""
    printed: dict[int, str] = {}
    for layout in layouts:
        lines = list(layout.footer)
        if layout.header and "Answer Key" in layout.header[0]:
            lines += [text for _column, _row, text in layout.body]
        for line in lines:
            for number, label in _KEY_TOKEN.findall(line):
                assert int(number) not in printed  # each key printed exactly once
                printed[int(number)] = label
    return printed


@pytest.mark.parametrize(("style", "count", "seeds"), [("clean", 5, 600), ("dense", 3, 300)])
def test_question_counts_hold_for_every_seed(style: str, count: int, seeds: int) -> None:
    low, high = {"clean": (8, 12), "dense": (24, 30)}[style]
    for seed in range(seeds):
        _rng, quiz, layouts = pages_module._layouts(seed, count, style, 96, None)  # type: ignore[arg-type]
        assert all(low <= len(questions) <= high for questions in quiz.pages), seed
        numbers = [q.number for questions in quiz.pages for q in questions]
        assert numbers == list(range(1, len(numbers) + 1))
        for layout in layouts:  # nothing is drawn outside the question area
            assert all(0 <= row < layout.rows and column < layout.columns
                       for column, row, _text in layout.body)


def test_pages_that_used_to_run_short_reach_the_minimum() -> None:
    # Seeds whose pages once stopped at 5-7 questions after six failed placements.
    for seed, count, index in ((66, 5, 1), (87, 5, 4), (140, 5, 0)):
        page = generate_pages(seed, count, "clean", key_page=False)[index]
        assert 8 <= len(page.truth.questions) <= 12


def test_minimum_count_is_refused_rather_than_silently_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With only long questions left to fall back on, the planner refuses instead of
    # producing a page below the contract range.
    def too_long(rng: random.Random) -> pages_module._Draft:
        return pages_module._Draft("word " * 400, ["1", "2", "3", "4"], 0, "arith")

    monkeypatch.setattr(pages_module, "_SHORT_FAMILIES", (too_long,))
    with pytest.raises(ValueError, match="minimum question count"):
        pages_module._layouts(115, 5, "clean", 96, False)  # a page needs the fallback


def test_geometry_too_small_for_a_style_is_refused() -> None:
    with pytest.raises(ValueError, match="too small for the dense layout"):
        pages_module._layouts(1, 1, "dense", 72, False)
    for dpi in (48, 72, 96, 150, 300):  # clean holds 8-12 questions at every allowed dpi
        _rng, quiz, _layouts = pages_module._layouts(3, 2, "clean", dpi, False)
        assert all(8 <= len(questions) <= 12 for questions in quiz.pages)


@pytest.mark.parametrize("key_page", [True, False, None])
def test_every_question_key_is_printed(key_page: bool | None) -> None:
    for seed in range(40):
        for count in (1, 2, 5, 12):
            _rng, quiz, layouts = pages_module._layouts(seed, count, "clean", 96, key_page)
            questions = [q for page in quiz.pages for q in page]
            assert _printed_keys(layouts) == {q.number: q.answer_key for q in questions}


def test_key_page_continues_into_the_second_dense_column() -> None:
    _rng, quiz, layouts = pages_module._layouts(0, 14, "dense", 96, True)
    key = layouts[-1]
    assert quiz.key_page and "Answer Key" in key.header[0]
    assert {column for column, _row, _text in key.body} == {0, 1}
    questions = [q for page in quiz.pages for q in page]
    assert _printed_keys(layouts) == {q.number: q.answer_key for q in questions}


def test_a_key_that_cannot_fit_one_page_is_never_truncated() -> None:
    # 20 clean pages hold ~200 questions; one key page prints at most 180 keys.
    with pytest.raises(ValueError, match="does not fit on one key page"):
        generate_pages(3, 20, "clean", key_page=True)
    chosen = [
        seed for seed in range(40)
        if pages_module._layouts(seed, 1, "clean", 96, None)[1].key_page
    ]
    assert chosen  # seeds that pick a key page for a short quiz ...
    for seed in chosen[:4]:
        _rng, quiz, layouts = pages_module._layouts(seed, 20, "clean", 96, None)
        assert not quiz.key_page  # ... fall back to footer keys for a long one
        assert len(layouts) == 20 and all(layout.footer for layout in layouts)
        assert layouts[0].header[0].endswith("Page 1 of 20")
        questions = [q for page in quiz.pages for q in page]
        assert _printed_keys(layouts) == {q.number: q.answer_key for q in questions}
    rendered = generate_pages(chosen[0], 20, "clean")
    assert len(rendered) == 20 and not any(p.truth.answer_key_page for p in rendered)


def test_key_page_presence() -> None:
    with_key = generate_pages(5, 2, "clean", key_page=True)
    assert len(with_key) == 3
    assert [page.truth.answer_key_page for page in with_key] == [False, False, True]
    key = with_key[-1]
    assert key.truth.questions == []
    assert key.truth.page == 3
    wrong = {number for page in with_key[:2] for number in page.printed_key_wrong}
    assert set(key.printed_key_wrong) == wrong

    without_key = generate_pages(5, 2, "clean", key_page=False)
    assert len(without_key) == 2
    assert not any(page.truth.answer_key_page for page in without_key)

    chosen = {generate_pages(seed, 1, "clean")[-1].truth.answer_key_page for seed in range(16)}
    assert chosen == {True, False}  # the seed picks footer box or key page


def test_printed_keys_match_derivation_except_deliberate_errors() -> None:
    pages_seen = wrong_pages = 0
    for seed in range(60):
        for page in generate_pages(seed, 3, "clean", key_page=False):
            pages_seen += 1
            assert len(page.printed_key_wrong) <= 2
            if page.printed_key_wrong:
                wrong_pages += 1
            numbers = {q.number for q in page.truth.questions}
            assert set(page.printed_key_wrong) <= numbers
            for question in page.truth.questions:
                derived = solve_question(question)
                assert (derived != question.answer_key) == (
                    question.number in page.printed_key_wrong
                )
    rate = wrong_pages / pages_seen
    assert 0.12 <= rate <= 0.28  # "~20% of pages"


def test_solve_question_rejects_unknown_families() -> None:
    choices = [Choice(label=label, text=str(i)) for i, label in enumerate("ABCD")]
    question = Question(number=1, stem="What colour is the sky?", choices=choices, answer_key="A")
    with pytest.raises(ValueError, match="unsolvable_question"):
        solve_question(question)
    arithmetic = Question(number=1, stem="What is 2 + 2?", choices=choices, answer_key="A")
    with pytest.raises(ValueError):
        solve_question(arithmetic)  # 4 is not among 0..3


def test_every_family_solves() -> None:
    families: set[str] = set()
    for page in generate_pages(11, 4, "clean", key_page=False):
        for question in page.truth.questions:
            stem = question.stem
            if stem.startswith("What is"):
                families.add("arith")
            elif stem.startswith("How many") and " are in " in stem:
                families.add("unit")
            elif stem.startswith("Which word"):
                families.add("word")
            elif stem.startswith("Which number"):
                families.add("extreme")
            elif "\n" in stem:
                families.add("passage")
            solve_question(question)
    assert families == {"arith", "unit", "word", "extreme", "passage"}


# Words outside a sentence's own category that would also complete it sensibly. None of
# them may ever be offered as a distractor, or a correct live reader fails the floor on a
# ground-truth flaw rather than on extraction.
_SECOND_COMPLETIONS: dict[str, frozenset[str]] = {
    "The ___ hooted from the barn at night.": frozenset({"whistle", "trumpet", "engine"}),
    "A small ___ crawled up the wall.": frozenset({"vine", "rose", "insect"}),
    "The ___ barked at the mail carrier.": frozenset({"voice", "sailor", "uncle"}),
    "The ___ sang in the tree every morning.": frozenset({"voice", "queen", "insect"}),
    "Write your name with a ___.": frozenset({"quill", "feather", "ink", "nail"}),
    "He climbed the ___ to reach the roof.": frozenset({"tree", "oak", "willow", "vine", "fence"}),
    "The rain stopped, so I closed my ___.": frozenset({"window"}),
    "She played a tune on her ___.": frozenset({"leaf", "voice"}),
    "Please fill the ___ with water.": frozenset({"vessel", "urn", "goblet", "lake", "canyon"}),
    "Put the letter in an ___ and seal it.": frozenset({"urn"}),
    "We paddled the ___ across the pond.": frozenset({"vessel", "yacht"}),
    "Climbers reached the peak of the ___.": frozenset({"tree", "oak"}),
    "We crossed the ___ over the stream.": frozenset({"log", "stone", "tree", "rope", "ladder"}),
    "The bird laid three eggs in its ___.": frozenset({"tree", "oak", "willow", "basket"}),
    "She peeled an ___ for lunch.": frozenset({"eel"}),
    "The soup needs one more chopped ___.": frozenset({"crab", "lobster", "eel", "leaf"}),
    "We scooped out the ___ and carved a face.": frozenset({"stone", "marble", "oak", "log"}),
    "Slice the ___ for the salad.": frozenset({"cactus", "leaf", "crab"}),
    "A ripe ___ fell from the tree.": frozenset({"acorn", "leaf"}),
}


def test_every_sentence_has_exactly_one_sensible_completion() -> None:
    sentences = {sentence: (word, category) for sentence, word, category in pages_module._SENTENCES}
    assert set(_SECOND_COMPLETIONS) <= set(sentences)
    for sentence, (word, category) in sentences.items():
        pool = pages_module._sentence_pool(sentence, category)
        assert len(pool) >= 3 and word not in pool
        assert not set(pool) & pages_module._CATEGORIES[category]
        assert not set(pool) & _SECOND_COMPLETIONS.get(sentence, frozenset()), sentence
    # The three sentences an unconstrained distractor could complete ("a flag with black and
    # white stripes", "snow on top of the van", "carved a flute") were replaced.
    stems = " ".join(sentences)
    assert "stripes" not in stems and "Snow covered" not in stems and "harvest fair" not in stems


def test_generated_word_questions_never_offer_a_second_completion() -> None:
    # The reviewer's scan: 300 seeds x 5 clean pages found 14 ambiguous questions before.
    word_questions = 0
    for seed in range(300):
        _rng, quiz, _layouts = pages_module._layouts(seed, 5, "clean", 96, False)
        for question in (q for page in quiz.pages for q in page):
            match = pages_module._WORD.match(question.stem)
            if match is None:
                continue
            word_questions += 1
            sentence = match[1].strip()
            answer = pages_module._SENTENCE_ANSWERS[sentence]
            texts = [choice.text for choice in question.choices]
            assert texts.count(answer) == 1
            # answer_key is the printed key (sometimes deliberately wrong); the derived
            # label is the ground truth and must point at the template word.
            derived = solve_question(question)
            assert [c.text for c in question.choices if c.label == derived] == [answer]
            offered = set(texts) - {answer}
            assert not offered & _SECOND_COMPLETIONS.get(sentence, frozenset()), sentence
            assert not offered & pages_module._NEVER_DISTRACTORS
    assert word_questions > 1000


def test_font_and_word_list_are_closed() -> None:
    assert set(GLYPHS) == {chr(code) for code in range(0x20, 0x7F)}
    assert all(len(rows) == GLYPH_HEIGHT for rows in GLYPHS.values())
    assert (GLYPH_WIDTH, GLYPH_HEIGHT) == (8, 13)
    assert len(WORDS) == 200 and len(set(WORDS)) == 200
    canvas = [bytearray(b"\xff" * 20) for _ in range(30)]
    draw_text(canvas, 2, 2, "Hié", scale=1)  # non-ASCII falls back to "?"
    assert any(0 in row for row in canvas)
    draw_text(canvas, 100, 100, "off canvas", scale=2)  # clipped, no error


def test_truth_models_are_strict() -> None:
    with pytest.raises(ValidationError):
        PageTruth.model_validate({"page": 1, "questions": [], "answer_key_page": False, "x": 1})
    with pytest.raises(ValidationError):
        Choice.model_validate({"label": "E", "text": "five"})
    page = generate_pages(7, 1, "clean", key_page=False)[0]
    Draft202012Validator(EXAM_OUTPUT_SCHEMA).validate(page.truth.model_dump(mode="json"))
    Draft202012Validator(STAGE1_OUTPUT_SCHEMA).validate(stage1_output([page]))


def test_vision_cases_validate_against_the_suite_input_schema(tmp_path: Path) -> None:
    rendered = generate_pages(9, 3, "clean", key_page=True)
    files = write_dataset(tmp_path / "vision", rendered, "vision", dataset_id="exam", seed=9)
    assert files.pngs == ()
    assert not list((tmp_path / "vision").glob("*.png"))  # never PNGs unless asked
    cases = TypeAdapter(list[EvaluationCase]).validate_json(files.cases.read_bytes())
    assert [case.case_id for case in cases] == ["exam-000", "exam-001", "exam-002", "exam-003"]
    validator = Draft202012Validator(VISION_INPUT_SCHEMA)
    for case, page in zip(cases, rendered, strict=True):
        validator.validate(case.task)
        assert isinstance(case.task, dict)
        images = case.task["images"]
        assert isinstance(images, list) and len(images) == 1
        literal = images[0]
        assert isinstance(literal, dict)
        payload = literal["$image"]
        assert isinstance(payload, dict)
        assert set(payload) == {"media_type", "base64"}
        assert base64.b64decode(str(payload["base64"])) == page.image
        assert case.task["synthetic"] is True
        assert case.expected == page.truth.model_dump(mode="json")
        assert case.metadata == {
            "synthetic": True,
            "source": "rendered-synthetic-page",
            "style": "clean",
            "generator_version": "v1",
        }
    # The stub form validates too: only media_type is required under $image.
    stub = {
        "$image": {
            "media_type": "image/png",
            "sha256": rendered[0].sha256,
            "width": 816,
            "height": 1056,
            "attached": 1,
        }
    }
    validator.validate({"synthetic": True, "page": 1, "images": [stub]})


def test_manifest_shape_and_png_option(tmp_path: Path) -> None:
    rendered = generate_pages(4, 2, "clean", key_page=True)
    files = write_dataset(tmp_path, rendered, "vision", dataset_id="m", seed=4, png=True)
    data = json.loads(files.manifest.read_text(encoding="utf-8"))
    assert set(data) == {
        "seed",
        "count",
        "style",
        "dpi",
        "generator_version",
        "pillow_version",
        "platform",
        "pages",
    }
    assert (data["seed"], data["count"], data["style"], data["dpi"]) == (4, 2, "clean", 96)
    assert data["pillow_version"] is None
    assert data["generator_version"] == "v1"
    assert [entry["sha256"] for entry in data["pages"]] == [page.sha256 for page in rendered]
    assert [entry["answer_key_page"] for entry in data["pages"]] == [False, False, True]
    assert [path.name for path in files.pngs] == ["page-000.png", "page-001.png", "page-002.png"]
    assert files.pngs[0].read_bytes() == rendered[0].image
    assert manifest_sha256(data) == manifest_sha256(manifest(rendered, seed=4))
    with pytest.raises(ValueError):
        write_dataset(tmp_path, [], "vision", dataset_id="empty")


def test_digitizer_cases_mirror_the_answer_key_fixture(tmp_path: Path) -> None:
    rendered = generate_pages(21, 2, "clean", key_page=True)
    files = write_dataset(tmp_path, rendered, "digitizer", dataset_id="dig", seed=21)
    cases = TypeAdapter(list[EvaluationCase]).validate_json(files.cases.read_bytes())
    assert [case.case_id for case in cases] == ["dig-000", "dig-001"]  # key page skipped
    fixture = json.loads(
        (Path(__file__).parents[1] / "examples/lectern/fixtures/answer-keys.json").read_text(
            encoding="utf-8"
        )
    )
    fixture_keys = set(fixture[0]["expected"]) if isinstance(fixture, list) else None
    for case, page in zip(cases, rendered[:2], strict=True):
        expected = case.expected
        assert isinstance(expected, dict)
        if fixture_keys is not None:
            assert set(expected) == fixture_keys
        assert expected["review_required"] is True
        assert expected["answer_keys"] == [
            {"id": f"q{q.number}", "answer": solve_question(q)} for q in page.truth.questions
        ]
        assert expected["disagreements"] == [f"q{n}" for n in page.printed_key_wrong]
        Draft202012Validator(VISION_INPUT_SCHEMA).validate(case.task)
    combined = digitizer_case(rendered, case_id="multi")
    assert isinstance(combined.task, dict)
    assert len(combined.task["images"]) == 3  # type: ignore[arg-type]
    assert combined.expected == digitizer_expected(rendered)
    with pytest.raises(ValueError):
        digitizer_case([rendered[-1]], case_id="key-only")


def test_cases_for_vision_includes_key_pages() -> None:
    rendered = generate_pages(21, 1, "clean", key_page=True)
    cases = cases_for(rendered, "vision", dataset_id="v")
    assert len(cases) == 2
    assert cases[1].expected == {"page": 2, "questions": [], "answer_key_page": True}


def test_page_model_is_frozen() -> None:
    page = generate_pages(7, 1, "clean", key_page=False)[0]
    with pytest.raises(ValidationError):
        page.index = 3
    assert isinstance(page, Page)


def test_missing_pillow_is_a_closed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(pages_module.importlib, "import_module", refuse)
    with pytest.raises(BenchError) as caught:
        generate_pages(1, 1, "scan")
    assert caught.value.code == "bench_extra_missing"
    with pytest.raises(BenchError):
        generate_pages(1, 1, "dense")
    generate_pages(1, 1, "clean")  # the stdlib renderer never needs Pillow


def test_scan_pages_render_with_pillow() -> None:
    pytest.importorskip("PIL")
    rendered = generate_pages(3, 2, "scan", key_page=False)
    for page in rendered:
        assert sniff(page.image) == ("image/png", 816, 1056)
        assert 8 <= len(page.truth.questions) <= 12
    # Same questions as the clean rendering of the same seed; only the pixels differ.
    clean = generate_pages(3, 2, "clean", key_page=False)
    assert [p.truth for p in rendered] == [p.truth for p in clean]
    assert rendered[0].image != clean[0].image
    data = manifest(rendered, seed=3)
    assert isinstance(data["pillow_version"], str)
    assert data["style"] == "scan"


def test_dense_pages_render_with_pillow() -> None:
    pytest.importorskip("PIL")
    for seed in range(4):
        page = generate_pages(seed, 1, "dense", key_page=False)[0]
        assert sniff(page.image) == ("image/png", 816, 1056)
        assert 24 <= len(page.truth.questions) <= 30
        for question in page.truth.questions:
            derived = solve_question(question)
            assert (derived != question.answer_key) == (question.number in page.printed_key_wrong)
