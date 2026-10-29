from rcon.source import Client
import time

time.sleep(2)

try:
    with Client('127.0.0.1', 27015, passwd='reed1') as c:
        print("Sending sm_ai_holdout bill...")
        response = c.run("sm_ai_holdout bill")
        print(f"Response: {response!r}")
except Exception as e:
    print(f"Error: {e}")