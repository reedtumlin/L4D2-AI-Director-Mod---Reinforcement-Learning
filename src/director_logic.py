"""
Pure (server-free) logic for the RL Director: action decoding + currency, the intensity
model, the reward function, and the observation vector.

Kept separate from the Gymnasium env so it can be unit-tested offline and so every number
that shapes the agent's behaviour lives in one place.

Calibration (c2m1_highway, Left 4 Bots 2 survivors, 8x timescale, team damage per 5 s step):
    calm (no SI)              mean  0.0   p90  0
    vanilla-like (2 SI)       mean  1.2   p90  4
    heavy SI (4 SI, 10 s)     mean  2.4   p90  0   (bots kill SI fast)
    horde (60 commons+panic)  mean  3.6   p90  7   1 incap
    tank + horde + SI         mean 14.5   p90 41   1 incap, 1 death
"""

from dataclasses import dataclass, field

import numpy as np

SPECIAL_CLASSES = ("smoker", "boomer", "hunter", "spitter", "jockey", "charger")

# ------------------------------------------------------------------
# ACTIONS
# ------------------------------------------------------------------
# action = [si_level, common_level, event]  ->  MultiDiscrete([5, 4, 4])

# (MaxSpecials, per-class limit, SpecialRespawnInterval seconds)
SI_LEVELS = (
    (0, 0, 60),
    (1, 1, 40),
    (2, 1, 25),
    (3, 2, 15),
    (4, 2, 8),
)
COMMON_LEVELS = (5, 15, 30, 50)  # CommonLimit

EVENT_NONE, EVENT_HORDE, EVENT_WITCH, EVENT_TANK = range(4)
EVENT_NAMES = ("none", "horde", "witch", "tank")
EVENT_COMMANDS = {
    EVENT_HORDE: "director_force_panic_event",
    EVENT_WITCH: "sm_rl_spawn witch",
    EVENT_TANK: "sm_rl_spawn tank",
}
EVENT_COSTS = {EVENT_NONE: 0, EVENT_HORDE: 8, EVENT_WITCH: 30, EVENT_TANK: 40}
MAX_WITCHES_ALIVE = 2  # the vanilla Director places only a few per map; stop witch spam

ACTION_NVEC = (len(SI_LEVELS), len(COMMON_LEVELS), len(EVENT_NAMES))

# Currency: earned per decision step, capped so the agent can't bank unlimited Tanks.
BUDGET_START = 10.0
BUDGET_PER_STEP = 1.0
BUDGET_MAX = 60.0
# With action masking (MaskablePPO) invalid events can't be chosen; this penalty is only a
# safety net for un-masked use (e.g. random/scripted policies during evaluation).
INVALID_EVENT_PENALTY = 0.1


def director_overwrite(si_level, common_level):
    """DirectorOptions override string for l4d2_directoroptions_overwrite."""
    max_specials, class_limit, respawn = SI_LEVELS[si_level]
    parts = [f"MaxSpecials={max_specials}", f"SpecialRespawnInterval={respawn}",
             f"CommonLimit={COMMON_LEVELS[common_level]}"]
    parts += [f"{c.capitalize()}Limit={class_limit}" for c in SPECIAL_CLASSES]
    return ";".join(parts)


def event_allowed(event, budget, state):
    """(allowed, reason). One Tank alive at a time, at most MAX_WITCHES_ALIVE witches."""
    if event == EVENT_NONE:
        return True, ""
    if budget < EVENT_COSTS[event]:
        return False, "unaffordable"
    if event == EVENT_TANK and state["tank_count"] > 0:
        return False, "tank_alive"
    if event == EVENT_WITCH and state["witch_count"] >= MAX_WITCHES_ALIVE:
        return False, "witch_cap"
    return True, ""


def action_mask(budget, state):
    """Flat boolean mask over MultiDiscrete(ACTION_NVEC) for MaskablePPO.

    SI and common pressure levels are always allowed; events are allowed only when
    event_allowed() says so, so the agent never picks an unaffordable/illegal event.
    """
    si = [True] * ACTION_NVEC[0]
    common = [True] * ACTION_NVEC[1]
    events = [event_allowed(e, budget, state)[0] for e in range(ACTION_NVEC[2])]
    return np.array(si + common + events, dtype=bool)


# ------------------------------------------------------------------
# INTENSITY
# ------------------------------------------------------------------
INTENSITY_DECAY = 0.75     # per 5 s step -> ~12 s half-life
DAMAGE_SCALE = 25.0        # team damage per step that alone saturates stress
INCAP_STRESS = 0.4
DEATH_STRESS = 0.6
SI_STRESS = 0.06           # per living special infected
TANK_STRESS = 0.25         # per living tank
COMMON_STRESS_MAX = 0.3    # commons contribution cap
COMMON_SCALE = 150.0


def effective_health(survivor):
    """Health that can still be lost before going down. Incapped/dead survivors have 0
    (an incapped survivor's ~300 'incap health' is not normal health)."""
    if not survivor["alive"] or survivor["incap"]:
        return 0.0
    return survivor["health"] + survivor["temp_health"]


def step_events(prev_state, state):
    """Team damage, new incaps and new deaths between two consecutive states."""
    damage, incaps, deaths = 0.0, 0, 0
    for a, b in zip(prev_state["survivors"], state["survivors"]):
        a_up = a["alive"] and not a["incap"]
        b_up = b["alive"] and not b["incap"]
        if a_up and b_up:
            damage += max(0.0, effective_health(a) - effective_health(b))
        elif a_up and not b_up:
            damage += effective_health(a)  # all remaining health was lost going down
        if a["alive"] and not a["incap"] and b["alive"] and b["incap"]:
            incaps += 1
        if a["alive"] and not b["alive"]:
            deaths += 1
    return damage, incaps, deaths


def stress(damage, incaps, deaths, state):
    s = (damage / DAMAGE_SCALE
         + INCAP_STRESS * incaps
         + DEATH_STRESS * deaths
         + SI_STRESS * state["si_count"]
         + TANK_STRESS * state["tank_count"]
         + min(COMMON_STRESS_MAX, state["common_count"] / COMMON_SCALE))
    return float(min(1.0, s))


# ------------------------------------------------------------------
# REWARD
# ------------------------------------------------------------------
TARGET_LOW = 0.25
TARGET_HIGH = 0.60
BAND_REWARD = 1.0
BORED_PENALTY_AT_ZERO = 0.5       # reward at intensity 0 is BAND_REWARD - 1.5 = -0.5
OVERWHELM_PENALTY_AT_ONE = 1.0    # reward at intensity 1 is -1.0
INCAP_PENALTY = 1.0
DEATH_PENALTY = 5.0
WIPE_PENALTY = 20.0
COMPLETE_BONUS = 10.0
COMPLETE_BONUS_PER_ALIVE = 2.5


def band_reward(intensity):
    """+1 inside [TARGET_LOW, TARGET_HIGH], linear fall-off outside it."""
    if intensity < TARGET_LOW:
        slope = (BAND_REWARD + BORED_PENALTY_AT_ZERO) / TARGET_LOW
        return BAND_REWARD - slope * (TARGET_LOW - intensity)
    if intensity > TARGET_HIGH:
        slope = (BAND_REWARD + OVERWHELM_PENALTY_AT_ONE) / (1.0 - TARGET_HIGH)
        return BAND_REWARD - slope * (intensity - TARGET_HIGH)
    return BAND_REWARD


def compute_reward(intensity, incaps, deaths, invalid_event, wiped, completed, alive):
    terms = {
        "band": band_reward(intensity),
        "incap": -INCAP_PENALTY * incaps,
        "death": -DEATH_PENALTY * deaths,
        "invalid": -INVALID_EVENT_PENALTY if invalid_event else 0.0,
        "wipe": -WIPE_PENALTY if wiped else 0.0,
        "complete": (COMPLETE_BONUS + COMPLETE_BONUS_PER_ALIVE * alive) if completed else 0.0,
    }
    return float(sum(terms.values())), terms


# ------------------------------------------------------------------
# DIRECTOR STATE (per episode)
# ------------------------------------------------------------------
OBS_SIZE = 22


@dataclass
class DirectorTracker:
    """Everything the RL Director remembers within one episode."""
    max_steps: int
    budget: float = BUDGET_START
    intensity: float = 0.0
    last_stress: float = 0.0
    steps: int = 0
    steps_since_damage: int = 0
    steps_since_event: int = 0
    last_progress: float = 0.0
    progress_delta: float = 0.0
    last_action: tuple = (0, 0, 0)
    stats: dict = field(default_factory=lambda: {
        "damage": 0.0, "incaps": 0, "deaths": 0, "events": {n: 0 for n in EVENT_NAMES},
        "invalid_events": 0, "intensity_sum": 0.0, "in_band_steps": 0,
        "over_steps": 0, "under_steps": 0,
    })

    def update(self, prev_state, state, action, event_fired, invalid_event):
        """Advance one decision step. Returns (damage, incaps, deaths)."""
        damage, incaps, deaths = step_events(prev_state, state)
        self.last_stress = stress(damage, incaps, deaths, state)
        self.intensity = INTENSITY_DECAY * self.intensity + (1 - INTENSITY_DECAY) * self.last_stress

        self.steps += 1
        self.steps_since_damage = 0 if damage > 0 else self.steps_since_damage + 1
        self.steps_since_event = 0 if event_fired else self.steps_since_event + 1
        self.progress_delta = state["progress"] - self.last_progress
        self.last_progress = state["progress"]
        self.last_action = tuple(int(a) for a in action)
        self.budget = min(BUDGET_MAX, self.budget + BUDGET_PER_STEP)

        st = self.stats
        st["damage"] += damage
        st["incaps"] += incaps
        st["deaths"] += deaths
        st["invalid_events"] += int(invalid_event)
        st["intensity_sum"] += self.intensity
        if self.intensity < TARGET_LOW:
            st["under_steps"] += 1
        elif self.intensity > TARGET_HIGH:
            st["over_steps"] += 1
        else:
            st["in_band_steps"] += 1
        return damage, incaps, deaths

    def summary(self):
        n = max(1, self.steps)
        st = self.stats
        return {
            "steps": self.steps,
            "damage": st["damage"], "incaps": st["incaps"], "deaths": st["deaths"],
            "mean_intensity": st["intensity_sum"] / n,
            "in_band_frac": st["in_band_steps"] / n,
            "over_frac": st["over_steps"] / n,
            "under_frac": st["under_steps"] / n,
            "invalid_events": st["invalid_events"],
            **{f"events_{k}": v for k, v in st["events"].items()},
        }

    def observation(self, state, elapsed_frac):
        survivors = state["survivors"]
        alive = [s for s in survivors if s["alive"]]
        up = [s for s in alive if not s["incap"]]
        eff = [effective_health(s) for s in survivors] or [0.0]
        ammo = [min(1.0, (s["ammo"] + s["reserve_ammo"]) / 400.0) for s in up] or [0.0]
        obs = [
            len(alive) / 4.0,
            (len(alive) - len(up)) / 4.0,                       # incapped fraction
            float(np.mean([effective_health(s) for s in up])) / 100.0 if up else 0.0,
            min(eff) / 100.0,
            float(np.mean(ammo)),
            state["progress"],
            float(np.clip(self.progress_delta * 20.0, -1.0, 1.0)),
            min(1.5, state["si_count"] / 4.0),
            float(state["tank_count"]),
            state["witch_count"] / 2.0,
            min(1.5, state["common_count"] / 60.0),
            self.intensity,
            self.last_stress,
            min(1.0, self.steps_since_damage / 24.0),
            self.budget / BUDGET_MAX,
            elapsed_frac,
            self.last_action[0] / (ACTION_NVEC[0] - 1),
            self.last_action[1] / (ACTION_NVEC[1] - 1),
            self.last_action[2] / (ACTION_NVEC[2] - 1),
            float(self.budget >= EVENT_COSTS[EVENT_TANK]),
            float(self.budget >= EVENT_COSTS[EVENT_HORDE]),
            min(1.0, self.steps_since_event / 24.0),
        ]
        assert len(obs) == OBS_SIZE
        return np.asarray(obs, dtype=np.float32)
