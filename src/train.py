"""
Train the RL Director with MaskablePPO (PPO + action masking, from sb3-contrib).

    python src/train.py --timesteps 40000              # fresh run
    python src/train.py --timesteps 40000 --resume     # continue from models/ppo_director_latest.zip
    tensorboard --logdir ppo_l4d2_tensorboard          # watch curves (director/* = episode metrics)

Throughput is roughly 4,600 env steps per hour at TIMESCALE=8.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from sb3_contrib import MaskablePPO
from stable_baselines3.common.callbacks import BaseCallback, CheckpointCallback
from stable_baselines3.common.monitor import Monitor

from test_pipeline import L4D2DirectorEnv

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models"
LATEST = MODEL_DIR / "ppo_director_latest.zip"
TB_DIR = ROOT / "ppo_l4d2_tensorboard"


class DirectorMetricsCallback(BaseCallback):
    """Log per-episode Director stats to TensorBoard and append them to a JSONL file."""

    KEYS = ("mean_intensity", "in_band_frac", "over_frac", "under_frac", "incaps", "deaths",
            "damage", "completed", "wiped", "progress_end", "game_seconds", "invalid_events",
            "events_horde", "events_witch", "events_tank")

    def __init__(self, jsonl_path):
        super().__init__()
        self.jsonl_path = Path(jsonl_path)
        self.recent = []

    def _on_step(self):
        for info in self.locals.get("infos", []):
            stats = info.get("episode_stats")
            if not stats:
                continue
            ep = info.get("episode", {})  # added by Monitor: r = return, l = length
            row = {"timestep": self.num_timesteps, "time": time.time(),
                   "return": ep.get("r"), "length": ep.get("l"), **stats}
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            self.recent = (self.recent + [stats])[-20:]
            for k in self.KEYS:
                vals = [float(s[k]) for s in self.recent if k in s]
                if vals:
                    self.logger.record(f"director/{k}", float(np.mean(vals)))
        return True


def main():
    # Windows consoles/redirected files default to cp1252; never let a log line crash training.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--timesteps", type=int, default=40_000)
    ap.add_argument("--resume", action="store_true", help=f"continue from {LATEST.name}")
    ap.add_argument("--n-steps", type=int, default=512, help="env steps per PPO update (~7 min at 8x)")
    ap.add_argument("--checkpoint-every", type=int, default=2048)
    ap.add_argument("--quiet", action="store_true", help="don't print every env step")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    MODEL_DIR.mkdir(exist_ok=True)
    env = Monitor(L4D2DirectorEnv(verbose=not args.quiet))

    if args.resume and LATEST.exists():
        print(f"Resuming from {LATEST}")
        model = MaskablePPO.load(LATEST, env=env, tensorboard_log=str(TB_DIR))
    else:
        # MaskablePPO = PPO that only samples legal actions (env.action_masks()):
        # unaffordable events / a second Tank are never chosen.
        model = MaskablePPO(
            "MlpPolicy", env,
            n_steps=args.n_steps, batch_size=64, n_epochs=10,
            learning_rate=3e-4, gamma=0.99, gae_lambda=0.95,
            ent_coef=0.01,          # keep exploring event choices early on
            verbose=1, seed=args.seed, tensorboard_log=str(TB_DIR),
        )

    callbacks = [
        CheckpointCallback(save_freq=args.checkpoint_every, save_path=str(MODEL_DIR), name_prefix="ppo_director"),
        DirectorMetricsCallback(MODEL_DIR / "episodes.jsonl"),
    ]
    try:
        model.learn(total_timesteps=args.timesteps, callback=callbacks,
                    reset_num_timesteps=not args.resume, progress_bar=False)
    except KeyboardInterrupt:
        print("Training interrupted by user.")
    finally:
        model.save(LATEST)
        print(f"Saved {LATEST} ({model.num_timesteps} total timesteps)")
        env.close()


if __name__ == "__main__":
    main()
