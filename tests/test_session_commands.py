"""Command grammar: verbs fold case, JSON survives, prose never becomes a command."""

from __future__ import annotations

import re

import pytest

from architect.session import commands as commands_module
from architect.session.commands import (
    GLOBAL,
    HELP,
    REGISTRY_READ,
    VERBS,
    VOCABULARY,
    allowed,
    globals_for,
    help_for,
    parse,
    split_first,
)
from architect.session.state import State


def test_blank_lines_parse_to_none() -> None:
    assert parse("", State.READY) is None
    assert parse("   \n", State.REVIEW) is None


def test_verb_is_case_folded_but_arguments_are_not() -> None:
    cmd = parse("SET Budget.cost_usd 0.7\n", State.REVIEW)
    assert cmd is not None
    assert cmd.verb == "set"
    assert cmd.args == ("Budget.cost_usd", "0.7")
    assert cmd.rest == "Budget.cost_usd 0.7"
    assert cmd.raw == "SET Budget.cost_usd 0.7"


def test_json_rest_keeps_its_spaces() -> None:
    line = 'run demo {"review": "great value, would buy again"}'
    cmd = parse(line, State.READY)
    assert cmd is not None
    assert cmd.verb == "run"
    assert cmd.rest == 'demo {"review": "great value, would buy again"}'
    assert cmd.args[0] == "demo"
    path, value = split_first(cmd.rest)
    assert path == "demo"
    assert value == '{"review": "great value, would buy again"}'


def test_unbalanced_quotes_fall_back_to_whitespace_split() -> None:
    cmd = parse("set task it's fine", State.REVIEW)
    assert cmd is not None
    assert cmd.args == ("task", "it's", "fine")
    assert cmd.rest == "task it's fine"


def test_tab_after_the_verb_still_parses_as_the_command() -> None:
    """A tab is whitespace like a space; otherwise `approve<TAB>token` would be prose."""
    cmd = parse("approve\t3fa9c2d1", State.REVIEW)
    assert cmd is not None
    assert cmd.verb == "approve"
    assert cmd.args == ("3fa9c2d1",)
    assert cmd.rest == "3fa9c2d1"
    tabbed = parse('set\tbudget.cost_usd\t{"a": 1}', State.REVIEW)
    assert tabbed is not None
    assert tabbed.verb == "set"
    assert split_first(tabbed.rest) == ("budget.cost_usd", '{"a": 1}')
    assert split_first("  only  ") == ("only", "")
    assert split_first("") == ("", "")


@pytest.mark.parametrize("state", [State.REVIEW, State.RESULT, State.FLOOR])
@pytest.mark.parametrize("line", ['{"task": "approve now"}', "[1, 2]", '{"set": 1} set x 1'])
def test_json_leading_lines_are_prose(state: State, line: str) -> None:
    cmd = parse(line, state)
    assert cmd is not None
    assert cmd.verb == "text"
    assert cmd.rest == line


def test_unknown_first_token_is_prose_in_every_state() -> None:
    for state in State:
        cmd = parse("Label the sentiment of a review", state)
        assert cmd is not None
        assert cmd.verb == "text"
        assert cmd.rest == "Label the sentiment of a review"
        assert cmd.args == ("Label", "the", "sentiment", "of", "a", "review")


def test_model_text_verbs_are_only_verbs_when_typed_first() -> None:
    cmd = parse("please approve 3fa9c2d1", State.REVIEW)
    assert cmd is not None
    assert cmd.verb == "text"


def test_allowed_matrix() -> None:
    assert GLOBAL == frozenset({"help", "status", "spend", "profile", "quit", "exit"})
    for state in State:
        expected = VOCABULARY[state] | globals_for(state)
        for verb in VERBS:
            cmd = parse(verb, state)
            assert cmd is not None
            assert allowed(cmd, state) == (verb in expected), (state, verb)
    busy = {State.INFERRING, State.COMPILING, State.BENCHING}
    for state in busy:
        assert VOCABULARY[state] == frozenset({"cancel"})
        assert globals_for(state) == GLOBAL - {"profile"}
    for state in set(State) - busy:
        assert globals_for(state) == GLOBAL
    assert "set" not in VOCABULARY[State.READY]
    assert "describe" not in VOCABULARY[State.REVIEW]
    assert "approve" in VOCABULARY[State.REVIEW]
    assert "install" in VOCABULARY[State.RESULT]
    assert VOCABULARY[State.BOOT] == frozenset() and VOCABULARY[State.EXIT] == frozenset()


def test_registry_reads_are_shared_by_ready_review_and_result() -> None:
    assert REGISTRY_READ == frozenset({"list", "info", "floors", "run"})
    assert "REGISTRY_READ" in commands_module.__all__  # public, like GLOBAL/VERBS/VOCABULARY
    for state in (State.READY, State.REVIEW, State.RESULT):
        assert REGISTRY_READ <= VOCABULARY[state], state
    assert not REGISTRY_READ & VOCABULARY[State.FLOOR]
    assert help_for("run") == (
        "  " + "run <name|dir> {json} | run {json}".ljust(40)
        + " an installed artifact, or in RESULT the compiled one\n"
    )
    review_line = (
        "  list | info <name>     registry table / one entry     floors | run <name|dir> {json}"
    )
    assert review_line + "\n" in HELP[State.REVIEW]
    result_line = (
        "  run {json}             run this artifact from disk    run <name|dir> {json}      "
        "an installed one"
    )
    assert result_line + "\n" in HELP[State.RESULT]


def _listed_verbs(text: str) -> set[str]:
    """The first token of every help cell; cells are two-space columns or ' | ' items."""
    found: set[str] = set()
    for line in text.splitlines()[1:]:
        for cell in re.split(r"\s{2,}", line.strip()):
            for item in cell.split(" | "):
                head = item.split(" ", 1)[0]
                if head in VERBS:
                    found.add(head)
    return found


def test_help_lists_exactly_the_state_vocabulary() -> None:
    for state in State:
        text = HELP[state]
        assert text.endswith("\n") and text.isascii()
        assert all(len(line) <= 100 for line in text.splitlines())
        expected = (VOCABULARY[state] | globals_for(state)) - {"exit"}
        assert _listed_verbs(text) == expected, state


def test_ready_help_is_the_fixed_text() -> None:
    assert HELP[State.READY] == (
        "Type what the agent should do (I will ask before spending), or:\n"
        "  describe <text>        infer a Spec from text without asking\n"
        "  spec <file|{json}>     load an explicit Spec        load <file>   same, for saved"
        " proposals\n"
        "  list | info <name>     registry table / one entry   run <name|dir> {json}\n"
        "  rollback <name> [rev]  recompile <name>             floors | floor <dataset> [kind]\n"
        "  use model <id>         budget compile <usd> <s>     profile | spend | status |"
        " help [verb] | quit\n"
        "  bench smoke            free fake-provider suite     bench quick|full|arc [models] |"
        " bench list\n"
        # Phase 10 Section 13.2: the READY verb roles; rate is allowed wherever a
        # delivery can have happened (a reuse or forge run leaves the session in REVIEW).
        "  roles                  role router: policy, variants and evidence (Stage 4 still"
        " licenses)\n"
        "  rate good|bad          rate the last delivery in this session (content-free)\n"
    )


def test_floor_help_is_the_fixed_text() -> None:
    assert HELP[State.FLOOR] == (
        "Build the floor dataset; every drafted case is SYNTHETIC until you review it:\n"
        "  use <path>             read an operator-owned file    add {json}          one case"
        " you wrote\n"
        "  paste                  JSON lines until '.'           draft [n]           model cases"
        " (default 20)\n"
        "  review <id> ok|drop|expected {json}                   split               tuning /"
        " held-out ids\n"
        "  show [<id>]            screen, or one case in full    accept | back       register /"
        " leave unsaved\n"
        "  profile | spend | status | help [verb] | quit\n"
    )
    assert help_for("show") == (
        "  show [<id>]                              the screen again; in FLOOR, show <id> is one"
        " case in full\n"
    )
    assert help_for("draft") == (
        "  draft [n]                                one metered call proposing n SYNTHETIC cases"
        " (default 20)\n"
    )


def test_help_for_one_verb() -> None:
    assert help_for("APPROVE").startswith("  approve [<hash8>]")
    assert help_for("nope") == "no such command; type help\n"
    for verb in VERBS:
        line = help_for(verb)
        assert line.startswith(f"  {verb}"), verb
        assert line.endswith("\n")
        assert all(len(part) <= 100 for part in line.splitlines()), verb
