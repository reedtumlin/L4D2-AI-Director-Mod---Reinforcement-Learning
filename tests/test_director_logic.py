"""Offline tests for src/director_logic.py.  Run: python tests/test_director_logic.py  (or pytest)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import director_logic as dl  # noqa: E402


def surv(health=100, temp=0, alive=True, incap=False, ammo=50, reserve=300):
    return {"health": health, "temp_health": temp, "alive": alive, "incap": incap,
            "ammo": ammo, "reserve_ammo": reserve}


def state(survivors=None, si=0, tank=0, witch=0, common=0, progress=0.0):
    return {"survivors": survivors if survivors is not None else [surv() for _ in range(4)],
            "si_count": si, "tank_count": tank, "witch_count": witch,
            "common_count": common, "progress": progress}


def test_overwrite_sets_all_six_classes():
    s = dl.director_overwrite(4, 3)
    for c in ("Smoker", "Boomer", "Hunter", "Spitter", "Jockey", "Charger"):
        assert f"{c}Limit=2" in s
    assert "MaxSpecials=4" in s and "CommonLimit=50" in s
    assert "MaxSpecials=0" in dl.director_overwrite(0, 0)


def test_incap_health_is_not_counted_as_healing_or_damage():
    prev = state([surv(30), surv(), surv(), surv()])
    cur = state([surv(health=300, incap=True), surv(), surv(), surv()])
    damage, incaps, deaths = dl.step_events(prev, cur)
    assert incaps == 1 and deaths == 0
    assert damage == 30  # lost the remaining 30 hp going down, not -270
    # staying incapped and bleeding out is neither damage nor a new incap
    cur2 = state([surv(health=250, incap=True), surv(), surv(), surv()])
    assert dl.step_events(cur, cur2) == (0.0, 0, 0)


def test_temp_health_counts_as_health():
    prev = state([surv(50, temp=40), surv(), surv(), surv()])
    cur = state([surv(50, temp=10), surv(), surv(), surv()])
    assert dl.step_events(prev, cur)[0] == 30


def test_death_detected():
    prev = state([surv(health=200, incap=True), surv(), surv(), surv()])
    cur = state([surv(health=0, alive=False), surv(), surv(), surv()])
    assert dl.step_events(prev, cur) == (0.0, 0, 1)


def test_band_reward_shape():
    assert dl.band_reward(0.4) == 1.0
    assert abs(dl.band_reward(0.0) - -0.5) < 1e-9
    assert abs(dl.band_reward(1.0) - -1.0) < 1e-9
    assert dl.band_reward(0.1) < dl.band_reward(0.2) < 1.0
    assert dl.band_reward(0.9) < dl.band_reward(0.7) < 1.0


def test_currency_and_tank_cap():
    st = state()
    assert dl.event_allowed(dl.EVENT_TANK, 39, st) == (False, "unaffordable")
    assert dl.event_allowed(dl.EVENT_TANK, 40, st) == (True, "")
    assert dl.event_allowed(dl.EVENT_TANK, 60, state(tank=1)) == (False, "tank_alive")
    assert dl.event_allowed(dl.EVENT_NONE, 0, st) == (True, "")


def test_action_mask_matches_event_rules():
    n_si, n_cl, n_ev = dl.ACTION_NVEC
    m = dl.action_mask(budget=9, state=state())
    assert m.shape == (n_si + n_cl + n_ev,) and m.dtype == bool
    assert m[: n_si + n_cl].all()                       # pressure levels always allowed
    assert list(m[n_si + n_cl:]) == [True, True, False, False]  # none, horde(8), witch(30), tank(40)
    m = dl.action_mask(budget=35, state=state())
    assert list(m[n_si + n_cl:]) == [True, True, True, False]   # witch affordable, tank not
    m = dl.action_mask(budget=60, state=state(tank=1))
    assert list(m[n_si + n_cl:]) == [True, True, True, False]   # tank already alive
    m = dl.action_mask(budget=60, state=state(witch=2))
    assert list(m[n_si + n_cl:]) == [True, True, False, True]   # witch cap reached
    m = dl.action_mask(budget=0, state=state())
    assert list(m[n_si + n_cl:]) == [True, False, False, False]  # "none" is always available


def test_budget_caps():
    tr = dl.DirectorTracker(max_steps=200)
    s = state()
    for _ in range(200):
        tr.update(s, s, (0, 0, 0), False, False)
    assert tr.budget == dl.BUDGET_MAX


def run_scenario(dmg_per_step, si=0, tank=0, common=0, steps=30):
    """Steady-state intensity for a constant pressure level."""
    tr = dl.DirectorTracker(max_steps=steps)
    prev = state(si=si, tank=tank, common=common)
    for _ in range(steps):
        cur = state([surv(100 - dmg_per_step / 4) for _ in range(4)], si=si, tank=tank, common=common)
        tr.update(prev, cur, (0, 0, 0), False, False)
        prev = state(si=si, tank=tank, common=common)  # "healed" back so damage stays constant
    return tr.intensity


def test_calibration_points_land_where_intended():
    calm = run_scenario(0, si=0, common=10)
    vanilla = run_scenario(1.2, si=2, common=30)
    heavy = run_scenario(2.4, si=4, common=30)
    chaos = run_scenario(14.5, si=4, tank=1, common=60)
    assert calm < dl.TARGET_LOW                       # boring
    assert dl.TARGET_LOW <= vanilla <= dl.TARGET_HIGH  # fine
    assert dl.TARGET_LOW <= heavy <= dl.TARGET_HIGH    # challenging but OK
    assert chaos > dl.TARGET_HIGH                     # overwhelming
    print(f"    steady-state intensity: calm {calm:.2f} vanilla {vanilla:.2f} heavy {heavy:.2f} chaos {chaos:.2f}")


def test_observation_shape_and_range():
    tr = dl.DirectorTracker(max_steps=200)
    s = state([surv(), surv(incap=True, health=300), surv(alive=False, health=0), surv(20, temp=30)],
              si=3, tank=1, common=40, progress=0.5)
    tr.update(state(), s, (4, 3, 3), True, False)
    obs = tr.observation(s, 0.25)
    assert obs.shape == (dl.OBS_SIZE,)
    assert (obs >= -1.0).all() and (obs <= 1.5).all(), obs


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"{len(tests)} passed")
