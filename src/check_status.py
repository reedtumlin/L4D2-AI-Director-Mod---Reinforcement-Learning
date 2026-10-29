from rcon.source import Client
import time

print("Waiting 2 seconds to ensure server is ready...")
time.sleep(2)

try:
    with Client('127.0.0.1', 27015, passwd='reed1') as c:
        print("\n=== PLUGINS ===")
        print(c.run("sm plugins list"))
        print("\n=== EXTENSIONS ===")
        print(c.run("sm exts list"))
except Exception as e:
    print(f"Could not connect: {e}")
    print("Make sure the dedicated server is currently running!")