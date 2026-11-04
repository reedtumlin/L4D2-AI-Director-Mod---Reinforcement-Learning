"""
L4D2 RL Director Training Pipeline
Trains a PPO agent to control the Director on c2m1_highway.
"""

import json
import subprocess
import time
from pathlib import Path

import numpy as np
import gymnasium as gym
from gymnasium import spaces
from rcon.source import Client
from stable_baselines3 import PPO

# ============================================================
# CONFIGURATION
# ============================================================

SRCDS_DIR = Path(r"C:\l4d2_server")
SRCDS_EXE = SRCDS_DIR / "srcds.exe"
GAME_DIR = SRCDS_DIR / "left4dead2"
STATE_FILE = GAME_DIR / "addons" / "sourcemod" / "logs" / "rl_state.json"
CONSOLE_LOG = GAME_DIR / "console.log"

SERVER_IP = "127.0.0.1"
RCON_PORT = 27015
RCON_PASSWORD = "reed1"
RCON_TIMEOUT = 30.0

LAUNCH_MAP = "c2m1_highway"

SERVER_BOOT_TIMEOUT = 120
MAP_LOAD_TIMEOUT = 60
STATE_TIMEOUT = 20
SURVIVOR_SPAWN_TIMEOUT = 120

# Game speed. Verified stable up to 8x (bots still clear the map, Director still spawns).
# Must be reset to 1 before any map load: map loads at timescale > 1 crash srcds.
TIMESCALE = 8.0

# Both measured in GAME seconds, so they mean the same thing at any TIMESCALE.
DECISION_INTERVAL = 5
EPISODE_MAX_TIME = 300

# Furthest-survivor flow / map max flow at which the map counts as completed.
PROGRESS_COMPLETE = 0.99

MIN_SURVIVORS = 3 
TARGET_SURVIVORS = 4

# Stuck detection: furthest-survivor flow must advance this much within STUCK_STEPS decisions
STUCK_FLOW_DELTA = 50.0
STUCK_STEPS = 6

# ============================================================
# SERVER MANAGEMENT HELPERS
# ============================================================

def kill_stale_processes():
    subprocess.run(["taskkill", "/F", "/IM", "srcds.exe"], capture_output=True, check=False)
    time.sleep(3) 

def launch_server():
    if not SRCDS_EXE.is_file():
        raise FileNotFoundError(f"Dedicated server executable not found: {SRCDS_EXE}")

    cmd = [
        str(SRCDS_EXE),
        "-game", "left4dead2",
        "-console",
        "-condebug",
        "-insecure",
        "-ip", "0.0.0.0",
        "-port", str(RCON_PORT),
        "+sv_lan", "0",
        "+sv_allow_lobby_connect_only", "0",
        "+maxplayers", "8", 
        "+survivor_limit", "8",
        "+map", LAUNCH_MAP,
        "+rcon_password", RCON_PASSWORD,
        "+sv_cheats", "1",
        "+sb_all_bot_game", "1",
        "+allow_all_bot_survivor_team", "1",
        "+sb_all_bot_team", "0",
        "+sv_idle_timeout", "0",
        "+motdfile", "nomotd.txt",  # Points to a blank file to disable the MOTD for spectators
    ]
    print(f"[Launch] Starting server window on {LAUNCH_MAP}...")
    return subprocess.Popen(cmd, cwd=str(SRCDS_DIR))

def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    print("[Shutdown] Terminating server...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

def wait_for_sourcemod(timeout=SERVER_BOOT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with Client(SERVER_IP, RCON_PORT, passwd=RCON_PASSWORD, timeout=RCON_TIMEOUT) as client:
                response = client.run("sm version")
                if response and "SourceMod" in response:
                    return True
        except Exception:
            time.sleep(2)
    return False

def print_console_log():
    if CONSOLE_LOG.exists():
        print("[Debug] Last 10 lines of console.log:")
        try:
            with open(CONSOLE_LOG, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                for line in lines[-10:]:
                    print(f"  SRV> {line.strip()}")
        except Exception as e:
            print(f"[Debug] Could not read console.log: {e}")

# ============================================================
# STATE READER
# ============================================================

class StateReader:
    def __init__(self, state_path):
        self.state_path = Path(state_path)

    def open(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.state_path.is_file():
                return
            time.sleep(0.25)
        raise FileNotFoundError(f"State file was not created: {self.state_path}")

    # Fields written by rl_state_logger v2 (sourcemod/scripting/rl_state_logger.sp).
    # Defaults keep the reader working against the v1 plugin.
    SURVIVOR_FIELDS = ("health", "temp_health", "x", "y", "z", "ammo", "reserve_ammo", "flow")
    SPECIAL_CLASSES = ("smoker", "boomer", "hunter", "spitter", "jockey", "charger")

    @classmethod
    def _normalize(cls, state):
        state["special_count"] = max(0, int(state["special_count"]))
        state["time"] = float(state.get("time", 0.0))
        for key in ("si_count", "tank_count", "witch_count", "common_count"):
            state[key] = int(state.get(key, 0))
        for key in ("flow", "max_flow", "progress"):
            state[key] = float(state.get(key, 0.0))
        state["left_safe_area"] = bool(state.get("left_safe_area", 0))
        state["map"] = str(state.get("map", ""))
        specials = state.get("specials") or {}
        state["specials"] = {c: int(specials.get(c, 0)) for c in cls.SPECIAL_CLASSES}
        for survivor in state["survivors"]:
            for key in cls.SURVIVOR_FIELDS:
                survivor[key] = float(survivor.get(key, 0))
            survivor["alive"] = bool(survivor.get("alive", survivor["health"] > 0))
            survivor["incap"] = bool(survivor.get("incap", 0))
        return state

    def wait_for_state(self, timeout=STATE_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                content = self.state_path.read_text(encoding="utf-8").strip()
                if content:
                    state = json.loads(content)
                    if isinstance(state.get("survivors"), list) and "special_count" in state:
                        return self._normalize(state)
            except (json.JSONDecodeError, OSError):
                pass
            time.sleep(0.2)
        raise TimeoutError(f"No valid state received within {timeout}s.")

    def wait_until(self, game_time, timeout=STATE_TIMEOUT):
        """Return the first state with time >= game_time.

        Also returns early if game time goes backwards (the map reloaded or changed),
        so callers can detect that instead of blocking.
        """
        deadline = time.monotonic() + timeout
        start_time = None
        while time.monotonic() < deadline:
            state = self.wait_for_state(timeout=max(0.5, deadline - time.monotonic()))
            if start_time is None:
                start_time = state["time"]
            if state["time"] >= game_time or state["time"] < start_time:
                return state
            time.sleep(0.05)
        raise TimeoutError(f"Game time never reached {game_time:.1f}s within {timeout}s.")

# ============================================================
# GYMNASIUM ENVIRONMENT
# ============================================================

class L4D2DirectorEnv(gym.Env):
    metadata = {"render_modes": ["human"], "render_fps": 4}

    def __init__(self):
        super().__init__()
        self.action_space = spaces.MultiDiscrete([4, 4, 4, 4])
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(25,), dtype=np.float32)

        self.server = None
        self.client = None
        self.reader = None
        self.start_time = 0          # game time at episode start
        self.wall_start = 0.0        # wall clock at episode start (for logging only)
        self.prev_state = None
        self.last_flow = None
        self.stuck_counter = 0
        self.last_game_time = None

    def _get_obs(self, state):
        obs = []
        survivors = state["survivors"]
        for i in range(4):
            if i < len(survivors):
                s = survivors[i]
                obs.extend([s["health"], s["temp_health"], s["x"], s["y"], s["z"], s["ammo"]])
            else:
                obs.extend([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        obs.append(float(state["special_count"]))
        return np.array(obs, dtype=np.float32)

    def _server_alive(self):
        return self.server is not None and self.server.poll() is None

    def _close_client(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None

    def _send_rcon(self, command, attempts=30, quiet=False):
        """RCON wrapper that reconnects across map loads (which drop RCON sockets).

        Raises RuntimeError if srcds has died, so callers can reboot instead of hanging.
        """
        for attempt in range(attempts):
            if not self._server_alive():
                raise RuntimeError("srcds is not running")
            try:
                if self.client is None:
                    self.client = Client(SERVER_IP, RCON_PORT, passwd=RCON_PASSWORD, timeout=RCON_TIMEOUT)
                    self.client.connect(login=True)
                return self.client.run(command)
            except Exception as e:
                if not quiet and attempt % 5 == 0:
                    print(f"[Env] RCON '{command}' failed ({type(e).__name__}); reconnecting ({attempt+1}/{attempts})...")
                self._close_client()
                time.sleep(1)
        raise RuntimeError(f"RCON command '{command}' failed after {attempts} attempts")

    def _apply_action(self, client, action):
        params = {
            "SmokerLimit": int(action[0]),
            "HunterLimit": int(action[1]),
            "BoomerLimit": int(action[2]),
            "MaxSpecials": int(action[3])
        }
        overwrite = ";".join(f"{k}={v}" for k, v in params.items())
        self._send_rcon(f'sm_cvar l4d2_directoroptions_overwrite "{overwrite}"')

    def _wait_for_survivors(self, deadline):
        """Poll the state file until TARGET_SURVIVORS (or a stable MIN_SURVIVORS) are present."""
        last_count, stalled = -1, 0
        while time.monotonic() < deadline:
            try:
                state = self.reader.wait_for_state()
            except TimeoutError:
                time.sleep(2)
                continue
            count = len(state["survivors"])
            if count >= TARGET_SURVIVORS:
                return state
            stalled = stalled + 1 if count == last_count else 0
            if stalled >= 3 and count >= MIN_SURVIVORS:
                print(f"[Env] Accepting {count} bots (server capped).")
                return state
            print(f"[Env] Currently {count} survivors. Waiting...")
            last_count = count
            time.sleep(2)
        print_console_log()
        raise RuntimeError(f"Failed to spawn bots within {SURVIVOR_SPAWN_TIMEOUT}s.")

    def _spawn_bots(self):
        """Headless episode start: add bots, then teleport them out of the start saferoom.

        Leaving the saferoom starts the Director and Left 4 Bots 2's auto-lead, so no game
        client / keyboard automation is needed (sm_rl_leave_saferoom in rl_director_tools.sp).
        """
        deadline = time.monotonic() + SURVIVOR_SPAWN_TIMEOUT
        # Bots do not carry over a map load, so this runs every episode.
        present = len(self.reader.wait_for_state()["survivors"])
        print(f"[Env] Adding {TARGET_SURVIVORS - present} survivor bots...")
        for _ in range(TARGET_SURVIVORS - present):
            self._send_rcon("sb_add")
            time.sleep(0.5)
        state = self._wait_for_survivors(deadline)
        print(f"[Env] {len(state['survivors'])} survivors present.")

        for distance in (600, 1000, 300):
            reply = self._send_rcon(f"sm_rl_leave_saferoom {distance}").strip()
            print(f"[Env] {reply}")
            for _ in range(10):
                time.sleep(1)
                state = self.reader.wait_for_state()
                if state["left_safe_area"]:
                    print("[Env] Survivors left the saferoom; Director is active.")
                    return state
        print_console_log()
        raise RuntimeError("Survivors never left the saferoom.")

    def _check_bot_movement(self, state):
        """Track map progress (furthest survivor flow) and nudge L4B2 if the team stalls."""
        flow = state["flow"]
        game_time = float(state.get("time", 0.0))

        # Check if the game logic itself froze (hibernation)
        time_frozen = False
        if self.last_game_time is not None and abs(game_time - self.last_game_time) < 0.1:
            time_frozen = True

        if self.last_flow is not None:
            # Fighting a horde/tank can legitimately pause progress, so require several stalled steps.
            if flow - self.last_flow < STUCK_FLOW_DELTA:
                self.stuck_counter += 1
            else:
                self.stuck_counter = 0

        if self.stuck_counter >= STUCK_STEPS or time_frozen:
            print("[Env] 🚨 BOTS ARE STUCK OR SERVER FROZEN.")
            self._send_rcon("scripted_user_func l4b,bots,lead")
            self.stuck_counter = 0

        self.last_flow = max(flow, self.last_flow or 0.0)
        self.last_game_time = game_time
        return state["progress"]

    def _boot_server(self):
        """Cold start: kill any srcds, launch a fresh one, wait for the state file."""
        self._close_client()
        stop_server(self.server)
        kill_stale_processes()
        if STATE_FILE.exists():
            try:
                STATE_FILE.unlink()
            except OSError:
                pass

        self.server = launch_server()
        if not wait_for_sourcemod():
            print_console_log()
            raise RuntimeError("Server failed to start.")

        print("[Env] Waiting for state file to be created...")
        wait_start = time.monotonic()
        while not STATE_FILE.exists():
            if not self._server_alive():
                print_console_log()
                raise RuntimeError("srcds crashed during boot.")
            if time.monotonic() - wait_start > 40:
                print_console_log()
                raise RuntimeError("State file failed to generate.")
            time.sleep(1)
        self.reader = StateReader(STATE_FILE)
        self.reader.open()
        print("[Env] Server up.")

    def _reload_map(self):
        """Warm reset: reload the map on the running server (~7s vs ~25s for a reboot).

        Uses `map`, not `changelevel`: same-map changelevel crashes srcds, `map` does not.
        Timescale must be back at 1 before the load or srcds crashes.
        """
        self._send_rcon("host_timescale 1")
        prev_time = self.reader.wait_for_state()["time"]
        try:
            self._send_rcon(f"map {LAUNCH_MAP}", attempts=1, quiet=True)
        except RuntimeError:
            pass  # the map load drops the RCON socket mid-reply
        self._close_client()

        deadline = time.monotonic() + MAP_LOAD_TIMEOUT
        while time.monotonic() < deadline:
            if not self._server_alive():
                raise RuntimeError("srcds died during map load")
            state = self.reader.wait_for_state()
            if state["time"] < prev_time:
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("Map reload timed out")
        time.sleep(3)  # let map activation (L4B2/VScript init) finish before sending commands
        self._send_rcon("status", quiet=True)

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        wall = time.monotonic()
        self.last_flow = None
        self.stuck_counter = 0
        self.last_game_time = None

        if self._server_alive() and self.reader is not None:
            try:
                self._reload_map()
            except RuntimeError as e:
                print(f"[Env] Warm reset failed ({e}); rebooting server.")
                self._boot_server()
        else:
            self._boot_server()

        # Clear the previous episode's Director overrides so every episode starts from vanilla.
        self._send_rcon('sm_cvar l4d2_directoroptions_overwrite ""')
        state = self._spawn_bots()
        self._send_rcon(f"host_timescale {TIMESCALE}")

        self.start_time = state["time"]
        self.wall_start = time.monotonic()
        self.prev_state = state
        print(f"[Env] Reset complete in {time.monotonic() - wall:.1f}s. Episode starting at {TIMESCALE:g}x!")
        return self._get_obs(state), {}

    def step(self, action):
        # Wait DECISION_INTERVAL of game time (DECISION_INTERVAL / TIMESCALE of wall time).
        # Server failures end the episode (truncated) instead of crashing training;
        # the next reset() reboots srcds.
        try:
            self._apply_action(self.client, action)
            state = self.reader.wait_until(self.prev_state["time"] + DECISION_INTERVAL)
        except (TimeoutError, RuntimeError) as e:
            print(f"[Env] Server problem mid-episode ({e}); truncating episode.")
            return self._get_obs(self.prev_state), 0.0, False, True, {"error": str(e)}

        elapsed = state["time"] - self.start_time

        survivors = state["survivors"]
        prev_survivors = self.prev_state["survivors"]

        alive = sum(1 for s in survivors if s["alive"])
        survival_reward = 0.5 * alive

        damage = 0.0
        for c, p in zip(survivors, prev_survivors):
            if c["health"] >= 0 and p["health"] >= 0:
                damage += max(0.0, p["health"] - c["health"])
        damage_penalty = -0.02 * damage

        specials = state["special_count"]
        if specials == 0: tension = -0.1
        elif specials <= 2: tension = 0.2
        elif specials <= 4: tension = 0.0
        else: tension = -0.5

        time_bonus = 0.01 * min(elapsed, EPISODE_MAX_TIME) / 10.0
        terminal_penalty = -10.0 if alive == 0 else 0.0

        reward = survival_reward + damage_penalty + tension + time_bonus + terminal_penalty

        terminated = bool(alive == 0)
        truncated = bool(elapsed >= EPISODE_MAX_TIME)
        
        # Map completed: survivors reached the end saferoom, or the server already moved on
        # (game time went backwards / different map).
        game_time = state["time"]
        map_changed = game_time < self.prev_state["time"] or (state["map"] and state["map"] != LAUNCH_MAP)
        completed = state["progress"] >= PROGRESS_COMPLETE or map_changed
        if completed and alive > 0:
            print("[Env] Map completed! Bots survived the level. Terminating episode.")
            terminated = True
            reward += 100.0 # Big reward for completing the level!

        progress = self._check_bot_movement(state)
        total_ammo = sum(s["ammo"] for s in survivors)

        self.prev_state = state
        obs = self._get_obs(state)
        wall = time.monotonic() - self.wall_start
        info = {"alive": alive, "specials": specials, "elapsed": elapsed, "wall": wall,
                "progress": state["progress"], "completed": completed, "stuck_counter": self.stuck_counter}

        print(f"[Step {elapsed:.0f}s game / {wall:.0f}s wall] Reward: {reward:.2f} | Alive: {alive} | Specials: {specials} | GameTime: {game_time:.0f}")
        print(f"   -> Action taken: Smoker={action[0]} Hunter={action[1]} Boomer={action[2]} MaxSpecials={action[3]}")
        print(f"   -> Progress: {progress:.1%} | Total Ammo: {total_ammo:.0f} | Stuck Counter: {self.stuck_counter}")

        return obs, reward, terminated, truncated, info

    def close(self):
        self._close_client()
        stop_server(self.server)

# ============================================================
# MAIN TRAINING LOOP
# ============================================================

if __name__ == "__main__":
    print("Initializing L4D2 Environment...")
    env = L4D2DirectorEnv()

    print("Setting up PPO Model...")
    model = PPO("MlpPolicy", env, verbose=1, tensorboard_log="./ppo_l4d2_tensorboard/")

    print("Starting Training...")
    try:
        model.learn(total_timesteps=500, progress_bar=True)
    except KeyboardInterrupt:
        print("Training interrupted by user.")
    finally:
        print("Saving model...")
        model.save("ppo_l4d2_director")
        env.close()
        print("Model saved as ppo_l4d2_director.zip")