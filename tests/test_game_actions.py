import pytest

from architect.games.actions import ActionError, parse_actions


def test_actions_preserve_order_and_allow_empty():
    assert parse_actions({"actions": ["up", "up"]}, {"up"}, 2) == ("up", "up")
    assert parse_actions({"actions": []}, {"up"}, 1) == ()


@pytest.mark.parametrize("value", [None, [], {"actions": "up"}, {"actions": [True]},
                                  {"actions": ["down"]}, {"actions": ["up"] * 3},
                                  {"actions": [], "license": True}])
def test_invalid_actions(value):
    with pytest.raises(ActionError, match="invalid_actions"):
        parse_actions(value, {"up"}, 2)


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5])
def test_invalid_limit(maximum):
    with pytest.raises(ValueError, match="invalid_action_limit"):
        parse_actions({"actions": []}, {"up"}, maximum)
