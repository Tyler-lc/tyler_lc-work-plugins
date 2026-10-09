"""Which result of a run's command a merge may bring back (run-mark --after), from the scenario's
state before and after the command. No platform: the acceptance rule on its own."""

from __future__ import annotations

import pytest

from ixmp_copies.platforms import run_outcome


def state(versions, default, solved):
    return {"model": "m", "scenario": "s", "versions": versions, "default": default, "solved": solved}


@pytest.mark.parametrize("before,after,accepted,new,in_place,says", [
    (state([], None, False), state([1], 1, True), True, True, False, None),             # a new name
    (state([1], 1, False), state([1, 2], 2, True), True, True, False, None),            # cloned, solved, default
    (state([1], 1, True), state([1, 2], 2, False), True, True, False, None),            # new, unsolved: merge decides
    (state([1], 1, False), state([1], 1, True), True, False, True, None),               # solved in place
    (state([1], 1, False), state([1, 2], 1, False), False, False, False, "still unsolved"),  # nothing solved
    (state([1], 1, True), state([1, 2], 1, True), False, False, False, "cannot be told"),   # forgot set_as_default
    (state([1], 1, True), state([1], 1, True), False, False, False, "Clone to a new version"),  # re-solved in place
    (state([1, 2], 2, True), state([1, 2], 1, True), False, False, False, "there before it"),  # an older version
    (state([1], 1, False), state([1], None, False), False, False, False, "no default version"),
], ids=["new_name", "new_version", "new_unsolved", "in_place", "unsolved_still", "forgot_default",
        "resolved_in_place", "older_version", "no_default"])
def test_run_outcome(before, after, accepted, new, in_place, says):
    out = run_outcome(before, after)
    assert (out["accepted"], out["new"], out["in_place"]) == (accepted, new, in_place), out
    assert out["before"] == before["versions"] and out["default"] == after["default"]
    if says:
        assert says in out["reason"], out["reason"]
    else:
        assert out["reason"] is None


def test_the_refusal_says_more_than_set_as_default():
    out = run_outcome(state([1], 1, True), state([1], 1, True))
    assert "set_as_default" in out["reason"] and "re-solve in place" in out["reason"]
