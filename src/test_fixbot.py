from rcon.source import Client
import time

time.sleep(2)

try:
    with Client('127.0.0.1', 27015, passwd='reed1') as c:
        for name in ["bill", "zoey", "francis", "louis"]:
            print(f"Sending sm_rl_spawnbot {name}...")
            response = c.run(f"sm_rl_spawnbot {name}")
            print(f"  Response: {response!r}")
            time.sleep(1)
except Exception as e:
    print(f"Error: {e}")
    print("Is the server running?")
