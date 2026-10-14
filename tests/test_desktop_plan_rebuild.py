"""The Plan screen's "Build with changes" (clarity spec, step 2): the app saves the next
build's settings (``set_override``), answers the open plan "no", then sends the same
``/make`` again. These pin the build-mode behaviour that relies on: a plan drawn from a
sentence picks the saved settings up on the next ``/make``, and a frozen plan
(``/make @file.json``) keeps its file's pass mark, budgets and model whatever was saved,
so the app offers "Build with changes" for those keys only on a plan that is not frozen.
"""

from __future__ import annotations

from pathlib import Path

from test_agent_build import DESCRIPTION, INTERVIEW, Harness, spec_json

from architect.agent.build import FROZEN_IGNORED, make, set_override


async def test_a_declined_plan_rebuilt_takes_the_saved_settings(tmp_path: Path) -> None:
    h = Harness(tmp_path, [INTERVIEW, spec_json(), INTERVIEW, spec_json()], ["", "", "", "no"] * 2)
    try:
        first = await make(DESCRIPTION, h.ctx)
        assert isinstance(first, dict) and first["cancelled"] is True and first["stage"] == "plan"
        assert h.rows()["floor"] == "held_out_tasks >= 1.00 on 'tasks'"
        # What the Plan sheet's Save sends while the plan waits, then the same /make again.
        set_override(h.ctx, "floor_limit", 0.9)
        await make(DESCRIPTION, h.ctx)
        assert h.rows()["floor"] == "held_out_tasks >= 0.90 on 'tasks'"
        assert (h.cards()[-1].data or {})["bar"] == 0.9
    finally:
        h.close()


def test_a_frozen_plan_ignores_the_pass_mark_budgets_and_model() -> None:
    # desktop/src/components/forge/PlanSheet.tsx FROZEN_FIXED mirrors this list.
    assert set(FROZEN_IGNORED) == {
        "floor_limit", "floor_file", "budget_usd", "budget_s", "compile_usd", "compile_s", "model"
    }
