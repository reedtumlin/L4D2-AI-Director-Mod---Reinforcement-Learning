"""
Prototype demo: watch the survivor bots play under the trained RL Director.

    python src/demo.py                              # trained Director, launches L4D2 and spectates
    python src/demo.py --vanilla                    # same, but with the stock L4D2 Director
    python src/demo.py --model models/ppo_director_8192_steps.zip --episodes 3
    python src/demo.py --no-client                  # server + Director only (connect yourself)

What happens:
  1. The dedicated server boots and the 4 survivor bots leave the saferoom (same as training).
  2. The L4D2 client is launched and connects (accept the -insecure / VAC popup if shown).
  3. rl_director_tools keeps you on the spectator team, following the lead bot in third person
     (--first-person to watch through the bot's eyes). Click to cycle between bots,
     Space to switch camera mode; if your bot dies the view jumps to a living one.
  4. Every 5 game-seconds the trained policy picks [SI pressure, common pressure, event].
     Events (horde / witch / tank) and periodic status are announced in the in-game chat.
  5. When the map is completed or the team wipes, the map reloads and a new run starts.
     Ctrl+C stops everything.

Runs at 1x speed by default so it looks like normal play. The policy is time-scale
independent (all its timing is in game seconds), so a model trained at 8x behaves the same.

Recording: capture the game window with Win+Alt+R (Xbox Game Bar) or OBS. Use --no-chat to
keep the Director announcements out of the footage.
If infected look invisible or rooms render as empty "void" while spectating, run
`sm_rl_specinfo` over RCON: "dist" should stay near 0 (see rl_director_tools.sp).
"""

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from sb3_contrib import MaskablePPO

import director_logic as dl
from test_pipeline import L4D2DirectorEnv, RCON_PORT

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = ROOT / "models" / "ppo_director_latest.zip"
STEAM_GAME_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Left 4 Dead 2")
STATUS_EVERY = 30  # game seconds between chat status lines

SI_NAMES = ("none", "light", "moderate", "heavy", "max")
COMMON_NAMES = ("sparse", "light", "normal", "dense")


def lan_ip():
    """This machine's LAN address (connecting to 127.0.0.1 can fail for the L4D2 client)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets are sent for a UDP connect
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def client_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq left4dead2.exe"],
                         capture_output=True, text=True, check=False).stdout
    return "left4dead2.exe" in out


def launch_client(ip):
    exe = STEAM_GAME_DIR / "left4dead2.exe"
    if not exe.is_file():
        print(f"[Demo] L4D2 client not found at {exe}. Start the game yourself and run in its console:\n"
              f"         connect {ip}:{RCON_PORT}")
        return
    if client_running():
        print(f"[Demo] L4D2 is already running. If it isn't connected, open its console (~) and run:\n"
              f"         connect {ip}:{RCON_PORT}")
        return
    print(f"[Demo] Launching L4D2 client -> {ip}:{RCON_PORT} (accept the insecure-mode popup if shown)")
    subprocess.Popen([str(exe), "-insecure", "-novid", "-console", "+connect", f"{ip}:{RCON_PORT}"],
                     cwd=str(STEAM_GAME_DIR))


class Announcer:
    """Posts Director decisions to the in-game chat via RCON `say`."""

    def __init__(self, env, enabled):
        self.env, self.enabled = env, enabled
        self.last_status = -STATUS_EVERY

    def say(self, msg):
        print(f"[Chat] {msg}")
        if self.enabled:
            try:
                self.env._send_rcon(f"say {msg}", attempts=2, quiet=True)
            except RuntimeError:
                pass

    def step(self, action, info, vanilla):
        if vanilla:
            if info["elapsed"] - self.last_status >= STATUS_EVERY:
                self.last_status = info["elapsed"]
                self.say(f"[Vanilla Director] intensity {info['intensity']:.2f}  progress {info['progress']:.0%}")
            return
        si, cl, ev = (int(a) for a in action)
        if info.get("event_fired"):
            self.say(f"[RL Director] Sending a {dl.EVENT_NAMES[ev].upper()}! "
                     f"(intensity {info['intensity']:.2f}, budget left {info['budget']:.0f})")
        if info["elapsed"] - self.last_status >= STATUS_EVERY:
            self.last_status = info["elapsed"]
            self.say(f"[RL Director] specials: {SI_NAMES[si]}, commons: {COMMON_NAMES[cl]} | "
                     f"intensity {info['intensity']:.2f} (target {dl.TARGET_LOW}-{dl.TARGET_HIGH}) "
                     f"| budget {info['budget']:.0f}")


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--vanilla", action="store_true", help="stock L4D2 Director (baseline), no model")
    ap.add_argument("--episodes", type=int, default=0, help="number of runs (0 = until Ctrl+C)")
    ap.add_argument("--timescale", type=float, default=1.0)
    ap.add_argument("--stochastic", action="store_true", help="sample actions instead of the policy's best guess")
    ap.add_argument("--no-client", action="store_true", help="don't launch the L4D2 client")
    ap.add_argument("--connect", default=None, help="IP for the client to connect to (default: LAN IP)")
    ap.add_argument("--no-chat", action="store_true", help="don't post Director decisions to in-game chat")
    ap.add_argument("--first-person", action="store_true", help="spectate through the bot's eyes (default: third person)")
    args = ap.parse_args()

    model = None
    if not args.vanilla:
        if not args.model.is_file():
            sys.exit(f"Model not found: {args.model}  (train one with src/train.py, or use --vanilla)")
        model = MaskablePPO.load(args.model)
        print(f"[Demo] Loaded {args.model} ({model.num_timesteps} training steps)")

    env = L4D2DirectorEnv(verbose=True, timescale=args.timescale, vanilla=args.vanilla)
    chat = Announcer(env, enabled=not args.no_chat)
    episode = 0
    try:
        obs, _ = env.reset()
        env._send_rcon(f"sm_rl_spectate_mode {4 if args.first_person else 5}")
        env._send_rcon("sm_rl_auto_spectate 1")
        if not args.no_client:
            launch_client(args.connect or lan_ip())
        else:
            print(f"[Demo] Server ready. In L4D2's console run:  connect {args.connect or lan_ip()}:{RCON_PORT}")

        while args.episodes == 0 or episode < args.episodes:
            episode += 1
            mode = "VANILLA" if args.vanilla else "RL"
            chat.say(f"[{mode} Director] Run {episode} starting.")
            chat.last_status = -STATUS_EVERY
            while True:
                if model is None:
                    action = env.action_space.sample()  # ignored by the env in vanilla mode
                else:
                    action, _ = model.predict(obs, action_masks=env.action_masks(),
                                              deterministic=not args.stochastic)
                obs, _reward, terminated, truncated, info = env.step(action)
                chat.step(action, info, args.vanilla)
                if terminated or truncated:
                    stats = info.get("episode_stats", {})
                    outcome = ("completed the map" if stats.get("completed") else
                               "were wiped out" if stats.get("wiped") else "ran out of time")
                    chat.say(f"[{mode} Director] Run {episode}: survivors {outcome}. "
                             f"Time in target intensity: {stats.get('in_band_frac', 0):.0%}, "
                             f"incaps {stats.get('incaps', 0)}, deaths {stats.get('deaths', 0)}.")
                    break
            if args.episodes and episode >= args.episodes:
                break
            time.sleep(5)  # let the spectator read the summary before the map reloads
            obs, _ = env.reset()
    except KeyboardInterrupt:
        print("\n[Demo] Stopped by user.")
    finally:
        env.close()
        print("[Demo] Server stopped. (Close the L4D2 client yourself.)")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
