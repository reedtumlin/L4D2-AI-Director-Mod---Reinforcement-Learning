import subprocess
import time
import os
from rcon.source import Client

# --- CONFIGURATION ---
SRCDS_PATH = r"C:\path\to\Left 4 Dead 2 Dedicated Server\srcds.exe"
SERVER_PORT = 27015
RCON_PASSWORD = "yourpassword"
LOG_FILE = r"C:\path\to\Left 4 Dead 2 Dedicated Server\left4dead2\console.log"
MAP = "c1m1_hotel"

def start_server():
    """Launch the dedicated server and wait for it to be ready."""
    cmd = [
        SRCDS_PATH,
        "-game", "left4dead2",
        "-console",
        "-insecure",
        "-port", str(SERVER_PORT),
        "-condebug",           # writes console.log
        "+maxplayers", "4",
        "+map", MAP,
        "+rcon_password", RCON_PASSWORD
    ]
    print("Starting server...")
    # Use CREATE_NEW_CONSOLE on Windows so the server window is visible
    return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)

def wait_for_server_ready(timeout=60):
    """Poll until the RCON port is accepting connections."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with Client('127.0.0.1', SERVER_PORT, passwd=RCON_PASSWORD) as client:
                client.run('status')
            print("Server is ready.")
            return True
        except Exception:
            time.sleep(2)
    print("Server did not become ready in time.")
    return False

def run_setup_commands():
    """Send the commands that put the game into spectator/bot mode and load your script."""
    with Client('127.0.0.1', SERVER_PORT, passwd=RCON_PASSWORD) as client:
        client.run('sv_cheats', '1')
        client.run('script_execute', 'rl_director_test')   # your VScript
        client.run('sb_all_bot_game', '1')
        # No need for jointeam on a dedicated server; there is no local client.
        # The bots will run the show automatically.
    print("Setup commands sent.")

def monitor_log_for_end():
    """Tail the log file until the map ends or all bots die."""
    # Ensure the log file exists before tailing
    if not os.path.exists(LOG_FILE):
        print(f"Log file not found: {LOG_FILE}")
        return "unknown"
    
    # Open in binary and seek to the end so we only see new lines
    with open(LOG_FILE, 'rb') as f:
        f.seek(0, 2)  # go to end
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5)
                continue
            text = line.decode('utf-8', errors='ignore')
            # --- END CONDITIONS ---
            # Level complete: look for the transition event
            if 'player_transitioned' in text and 'BOT' in text:
                print("Level complete (bots transitioned).")
                return "level_complete"
            # All bots dead: look for the last player_death followed by no survivors
            # A simple heuristic: if the server prints "No survivors left" or similar
            if 'No survivors left' in text or 'all survivors dead' in text.lower():
                print("All survivors dead.")
                return "all_dead"
            # Optional: stop after a fixed time for safety during testing
            # if time.time() - START_TIME > 600: return "timeout"

def stop_server(process):
    """Cleanly terminate the server process."""
    print("Stopping server...")
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    print("Server stopped.")

# --- MAIN LOOP ---
if __name__ == "__main__":
    server_proc = start_server()
    try:
        if not wait_for_server_ready():
            raise RuntimeError("Server never came up.")
        run_setup_commands()
        result = monitor_log_for_end()
        print(f"Episode ended: {result}")
    finally:
        stop_server(server_proc)