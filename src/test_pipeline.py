"""
L4D2 RL Director Training Pipeline
Trains a PPO agent to control the Director on c2m1_highway.
"""

import json
import subprocess
import time
import os
from pathlib import Path

import numpy as np
import gymnasium as gym
from gymnasium import spaces
from rcon.source import Client
from stable_baselines3 import PPO
import pyautogui
import pygetwindow as gw

# ============================================================
# CONFIGURATION
# ============================================================

SRCDS_DIR = Path(r"C:\l4d2_server")
SRCDS_EXE = SRCDS_DIR / "srcds.exe"
GAME_DIR = SRCDS_DIR / "left4dead2"
STATE_FILE = GAME_DIR / "addons" / "sourcemod" / "logs" / "rl_state.json"
CONSOLE_LOG = GAME_DIR / "console.log"

# Retail game paths
STEAM_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Left 4 Dead 2"
GAME_EXE = os.path.join(STEAM_DIR, "left4dead2.exe")

# YOUR ACTUAL LAN IP
LAN_IP = "192.168.0.157" 

SERVER_IP = "127.0.0.1"
RCON_PORT = 27015
RCON_PASSWORD = "reed1"
RCON_TIMEOUT = 30.0

LAUNCH_MAP = "c2m1_highway"

SERVER_BOOT_TIMEOUT = 120
STATE_TIMEOUT = 20
SURVIVOR_SPAWN_TIMEOUT = 120 
DECISION_INTERVAL = 5
EPISODE_MAX_TIME = 500

MIN_SURVIVORS = 3 
TARGET_SURVIVORS = 4

# ============================================================
# SERVER MANAGEMENT HELPERS
# ============================================================

def kill_stale_processes():
    subprocess.run(["taskkill", "/F", "/IM", "srcds.exe"], capture_output=True, check=False)
    subprocess.run(["taskkill", "/F", "/IM", "left4dead2.exe"], capture_output=True, check=False)
    time.sleep(2)

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

    def wait_for_state(self, timeout=STATE_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                content = self.state_path.read_text(encoding="utf-8").strip()
                if content:
                    state = json.loads(content)
                    if isinstance(state.get("survivors"), list) and "special_count" in state:
                        state["special_count"] = max(0, int(state["special_count"]))
                        for survivor in state["survivors"]:
                            for key in ("health", "temp_health", "x", "y", "z", "ammo"):
                                survivor.setdefault(key, 0)
                                survivor[key] = float(survivor[key])
                        return state
            except (json.JSONDecodeError, OSError):
                pass
            time.sleep(0.2)
        raise TimeoutError(f"No valid state received within {timeout}s.")

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
        self.start_time = 0
        self.prev_state = None
        self.last_avg_x = None
        self.stuck_counter = 0

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

    def _send_rcon(self, command):
        try:
            if self.client is None:
                raise ConnectionError("Client is None")
            return self.client.run(command)
        except Exception as e:
            print(f"[Env] RCON disconnected ({e}). Reconnecting...")
            try:
                self.client.close()
            except Exception:
                pass
            self.client = Client(SERVER_IP, RCON_PORT, passwd=RCON_PASSWORD, timeout=RCON_TIMEOUT)
            self.client.connect(login=True)
            return self.client.run(command)

    def _apply_action(self, client, action):
        params = {
            "SmokerLimit": int(action[0]),
            "HunterLimit": int(action[1]),
            "BoomerLimit": int(action[2]),
            "MaxSpecials": int(action[3])
        }
        overwrite = ";".join(f"{k}={v}" for k, v in params.items())
        self._send_rcon(f'sm_cvar l4d2_directoroptions_overwrite "{overwrite}"')

    def _automate_human_kickstart(self):
        """Launches the game, dismisses MOTD, and physically walks the human forward."""
        print("[Env] Launching retail L4D2 client...")
        
        client_cmd = [
            GAME_EXE,
            "-insecure",
            "-window",
            "-novid",
            "-nomotd",             
            "+connect", f"{LAN_IP}:27015"
        ]
        subprocess.Popen(client_cmd, cwd=STEAM_DIR)
        
        print("[Env] Waiting 15s for L4D2 window to appear...")
        time.sleep(15)
        
        try:
            l4d2_windows = gw.getWindowsWithTitle("Left 4 Dead 2")
            if l4d2_windows:
                win = l4d2_windows[0]
                win.activate()
                time.sleep(1)
                pyautogui.click(pyautogui.size()[0] // 2, pyautogui.size()[1] // 2)
                time.sleep(1)
                print("[Env] Spamming keys to bypass prompts and MOTD...")
                for _ in range(4):
                    pyautogui.press('enter')
                    time.sleep(0.5)
                    pyautogui.press('space')
                    time.sleep(0.5)
                    pyautogui.press('esc')
                    time.sleep(0.5)
                    pyautogui.press('h') 
                    time.sleep(0.5)
        except Exception as e:
            print(f"[Env] Automation error: {e}")

    def _spawn_bots(self):
        deadline = time.monotonic() + SURVIVOR_SPAWN_TIMEOUT
        last_count = -1
        stalled = 0

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

        time.sleep(5)

        print("[Env] Opening fresh RCON connection (with login handshake)...")
        with Client(SERVER_IP, RCON_PORT, passwd=RCON_PASSWORD, timeout=RCON_TIMEOUT) as c:
            warmup = c.run("sm version")
            time.sleep(1)

            # 1. Spawn 4 bots
            print("[Env] Spawning 4 bots...")
            for i in range(4):
                c.run("sb_add")
                time.sleep(1.5)
            
            time.sleep(3)
            
            # 2. Launch the game and dismiss VAC
            self._automate_human_kickstart()

            # 3. Wait for the human to fully spawn by checking the RCON STATUS for the LAN IP
            print("[Env] Waiting for human client to connect (checking RCON status for IP)...")
            spawn_deadline = time.monotonic() + 120 
            human_spawned = False
            
            while time.monotonic() < spawn_deadline:
                try:
                    status = c.run("status")
                    if "192.168.0.157" in status:
                        human_spawned = True
                        break
                except Exception as e:
                    print(f"[Env] Status check failed: {e}")
                time.sleep(2)

            if human_spawned:
                print("[Env] ✅ Human connected! Waiting 15s for intro to finish...")
                time.sleep(15) 
                
                # THE FIX: Physically hold W to walk the human out of the safe room
                print("[Env] Physically holding 'W' to walk the human out...")
                try:
                    l4d2_windows = gw.getWindowsWithTitle("Left 4 Dead 2")
                    if l4d2_windows:
                        l4d2_windows[0].activate()
                        time.sleep(1)
                    pyautogui.keyDown('w')
                    time.sleep(4) # Hold W for 4 seconds
                    pyautogui.keyUp('w')
                    print("[Env] Movement complete. Triggering L4B2 lead...")
                    c.run("scripted_user_func l4b,bots,lead")
                    time.sleep(3)
                except Exception as e:
                    print(f"[Env] Walk automation failed: {e}")
            else:
                print("[Env] ❌ Human never connected in time.")

            # 4. Use SourceMod plugin to move the human to Spectator mode. 
            print("[Env] Moving human to Spectator mode (keeping bot body)...")
            c.run("sm_rl_human2spec") 
            time.sleep(5)
            
            # 5. Check if we need to fill the team back up to 4 bots
            try:
                state = self.reader.wait_for_state()
                count = len(state["survivors"])
                if count < TARGET_SURVIVORS:
                    print(f"[Env] Bot count is {count}. Spawning {TARGET_SURVIVORS - count} extra bots...")
                    for _ in range(TARGET_SURVIVORS - count):
                        c.run("sb_add")
                        time.sleep(1.5)
            except Exception as e:
                print(f"[Env] Could not check bot count: {e}")

        print("[Env] Waiting for engine to settle...")
        time.sleep(5)

        print("[Env] Reconnecting for pipeline operations (with login)...")
        self.client = Client(SERVER_IP, RCON_PORT, passwd=RCON_PASSWORD, timeout=RCON_TIMEOUT)
        self.client.connect(login=True)

        while time.monotonic() < deadline:
            try:
                state = self.reader.wait_for_state()
            except TimeoutError:
                time.sleep(2)
                continue

            count = len(state["survivors"])
            if count >= TARGET_SURVIVORS:
                print(f"[Env] ✅ Successfully detected {count} bots.")
                return state

            stalled = stalled + 1 if count == last_count else 0
            if stalled >= 3 and count >= MIN_SURVIVORS:
                print(f"[Env] Accepting {count} bots (server capped).")
                return state

            print(f"[Env] Currently {count} survivors alive. Waiting...")
            time.sleep(4)
            last_count = count

        print("\n[DEBUG] Failed to detect bots. Printing relevant console logs:")
        print_console_log()
        raise RuntimeError("Failed to spawn bots after 120s.")

    def _check_bot_movement(self, state):
        avg_x = sum(s["x"] for s in state["survivors"]) / max(1, len(state["survivors"]))
        if self.last_avg_x is not None:
            movement = abs(avg_x - self.last_avg_x)
            if movement < 0.5:
                self.stuck_counter += 1
            else:
                self.stuck_counter = 0

        if self.stuck_counter >= 3:
            print("[Env] 🚨 BOTS ARE STUCK (no movement for 3 steps).")
            # L4B2 natively handles this
            self._send_rcon("scripted_user_func l4b,bots,lead")
            self.stuck_counter = 0

        self.last_avg_x = avg_x
        return avg_x

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        stop_server(self.server)
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self.reader = None
        self.last_avg_x = None
        self.stuck_counter = 0
        kill_stale_processes()

        self.server = launch_server()
        if not wait_for_sourcemod():
            raise RuntimeError("Server failed to start.")

        print(f"[Env] Waiting 15s for map ({LAUNCH_MAP}) to fully load...")
        time.sleep(15)

        if STATE_FILE.exists():
            try:
                STATE_FILE.unlink()
            except Exception:
                pass

        print("[Env] Waiting for state file to be created...")
        wait_start = time.monotonic()
        while not STATE_FILE.exists():
            if self.server.poll() is not None:
                print("\n[CRITICAL ERROR] srcds.exe crashed!")
                print_console_log()
                raise RuntimeError("Server crashed.")
            if time.monotonic() - wait_start > 40:
                print_console_log()
                raise RuntimeError("State file failed to generate.")
            time.sleep(2)

        print("[Env] State file detected!")

        self.reader = StateReader(STATE_FILE)
        self.reader.open()
        state = self._spawn_bots()

        # NOTE: Removed the conflicting vanilla cvars here. L4B2 handles the AI.

        self.start_time = time.monotonic()
        self.prev_state = state
        print("[Env] Reset complete. Episode starting!")
        return self._get_obs(state), {}

    def step(self, action):
        self._apply_action(self.client, action)
        time.sleep(DECISION_INTERVAL)

        try:
            state = self.reader.wait_for_state()
        except TimeoutError:
            return self._get_obs(self.prev_state), -10.0, True, False, {"error": "timeout"}

        elapsed = time.monotonic() - self.start_time

        survivors = state["survivors"]
        prev_survivors = self.prev_state["survivors"]

        alive = sum(1 for s in survivors if s["health"] > 0)
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

        avg_x = self._check_bot_movement(state)
        total_ammo = sum(s["ammo"] for s in survivors)

        self.prev_state = state
        obs = self._get_obs(state)
        info = {"alive": alive, "specials": specials, "elapsed": elapsed, "stuck_counter": self.stuck_counter}

        print(f"[Step {elapsed:.0f}s] Reward: {reward:.2f} | Alive: {alive} | Specials: {specials}")
        print(f"   -> Action taken: Smoker={action[0]} Hunter={action[1]} Boomer={action[2]} MaxSpecials={action[3]}")
        print(f"   -> Avg X-Pos: {avg_x:.1f} | Total Ammo: {total_ammo:.0f} | Stuck Counter: {self.stuck_counter}")

        return obs, reward, terminated, truncated, info

    def close(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
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