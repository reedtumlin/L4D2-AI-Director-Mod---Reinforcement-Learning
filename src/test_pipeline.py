"""
RL Director pipeline.
- Launches the dedicated server with SourceMod
- Waits for SourceMod + Director Options Unlocker to be ready
- Connects via RCON
- Runs a decision loop that changes Director options mid-level
- Detects episode end (placeholder) and shuts down
"""

import subprocess
import time
from pathlib import Path
from rcon.source import Client

# --- CONFIGURATION ---
SRCDS_DIR = Path(r"C:\l4d2_server")
SRCDS_EXE = SRCDS_DIR / "srcds.exe"
SERVER_IP = "192.168.0.153"      # Must match +ip below
RCON_PORT = 27015
RCON_PASSWORD = "reed1"
MAP = "c1m1_hotel"

DECISION_INTERVAL = 100            # seconds between RL decisions
EPISODE_MAX_TIME = 120            # hard timeout for a training episode
SERVER_BOOT_TIMEOUT = 120         # max seconds to wait for SourceMod to load


# --- SERVER MANAGEMENT ---
def kill_stale_processes():
    """Kill any leftover srcds before launching a new one."""
    subprocess.run(
        ["taskkill", "/F", "/IM", "srcds.exe"],
        capture_output=True, check=False,
    )
    time.sleep(2)


def launch_server():
    """Launch the dedicated server with SourceMod-compatible arguments."""
    cmd = [
        str(SRCDS_EXE),
        "-game", "left4dead2",
        "-console",
        "-insecure",
        "+ip", SERVER_IP,
        "+sv_lan", "1",
        "+sv_allow_lobby_connect_only", "0",
        "-port", str(RCON_PORT),
        "+maxplayers", "4",
        "+map", MAP,
        "+rcon_password", RCON_PASSWORD,
    ]
    print("[launch] starting dedicated server...")
    return subprocess.Popen(cmd, cwd=str(SRCDS_DIR))


def wait_for_sourcemod(timeout=SERVER_BOOT_TIMEOUT):
    """Wait until RCON connects AND SourceMod reports it is loaded."""
    print("[wait] waiting for server + SourceMod...")
    start = time.time()
    last_error = None
    while time.time() - start < timeout:
        try:
            with Client(SERVER_IP, RCON_PORT, passwd=RCON_PASSWORD) as c:
                resp = c.run("sm version")
                if resp and "SourceMod" in resp:
                    print("[wait] SourceMod is live.")
                    return True
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        time.sleep(3)
    print(f"[wait] gave up. Last error: {last_error}")
    return False


def stop_server(proc):
    print("[shutdown] terminating server...")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("[shutdown] done.")


# --- DIRECTOR CONTROL ---
def setup_episode(client):
    """One-time setup at the start of an episode."""
    client.run("sv_cheats", "1")
    client.run("sb_all_bot_game", "1")
    # Start with everything off; the agent will escalate as needed.
    apply_action(client, {"SmokerLimit": 0, "HunterLimit": 0, "MaxSpecials": 0})


def apply_action(client, params: dict):
    """
    Send an action to the Director via the SourceMod convar.
    Sent as ONE quoted string so semicolons survive the console parser.
    """
    overwrite = ";".join(f"{k}={v}" for k, v in params.items())
    command = f'sm_cvar l4d2_directoroptions_overwrite "{overwrite}"'
    client.run(command)
    print(f"[action] applied: {overwrite}")


def get_state(client) -> dict:
    """
    Read current game state for the RL agent.
    Placeholder — expand to parse health, position, ammo, etc.
    """
    status = client.run("status")
    return {"raw_status": status}


def compute_reward(state: dict, elapsed: float) -> float:
    """Placeholder reward function."""
    return 0.0


def episode_done(client, elapsed: float) -> bool:
    """Placeholder episode-end check."""
    return elapsed >= EPISODE_MAX_TIME


# --- MAIN LOOP ---
def main():
    kill_stale_processes()
    server = launch_server()

    try:
        if not wait_for_sourcemod():
            raise RuntimeError("SourceMod never became available.")

        with Client(SERVER_IP, RCON_PORT, passwd=RCON_PASSWORD) as client:
            setup_episode(client)

            # Demo action cycle — replace with RL agent output later
            demo_actions = [
                {"SmokerLimit": 0, "HunterLimit": 0, "MaxSpecials": 0},
                {"SmokerLimit": 1, "HunterLimit": 1, "MaxSpecials": 2},
                {"SmokerLimit": 2, "HunterLimit": 2, "BoomerLimit": 1, "MaxSpecials": 4},
                {"SmokerLimit": 0, "HunterLimit": 0, "MaxSpecials": 0},
            ]

            start_time = time.time()
            step = 0
            while not episode_done(client, time.time() - start_time):
                action = demo_actions[step % len(demo_actions)]
                apply_action(client, action)

                time.sleep(DECISION_INTERVAL)

                state = get_state(client)
                elapsed = time.time() - start_time
                reward = compute_reward(state, elapsed)
                print(f"[step {step}] elapsed={elapsed:.1f}s reward={reward:.2f}")

                step += 1

            print(f"[episode] complete after {step} decisions.")

    finally:
        stop_server(server)


if __name__ == "__main__":
    main()