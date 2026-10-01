"""Test: send a Director override to the server via RCON."""

from rcon.source import Client

RCON_HOST = "192.168.0.153"
RCON_PORT = 27015
RCON_PASSWORD = "reed1"

OVERRIDE = "SmokerLimit=100;HunterLimit=20;BoomerLimit=100;MaxSpecials=40"

with Client(RCON_HOST, RCON_PORT, passwd=RCON_PASSWORD) as client:
    command = f'sm_cvar l4d2_directoroptions_overwrite "{OVERRIDE}"'
    print(f"[send] {command}")
    response = client.run(command)
    print(f"[recv] {response!r}")
    print("[done]")