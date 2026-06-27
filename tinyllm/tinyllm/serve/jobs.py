"""A single background training job, driven from the admin console.

Training runs as a SUBPROCESS (`tinyllm train`) so it doesn't block the server's
event loop or fight it for the GIL, and so it gets its own memory. The admin
status endpoint polls `status()` (which parses the job log for the current step);
when the process exits cleanly the caller hot-reloads the new checkpoint.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_STEP = re.compile(r"step\s+(\d+)/(\d+)")


class TrainJob:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.log: Path | None = None
        self.out_dir: str | None = None
        self.schema_id: str | None = None
        self.total = 0
        self.reloaded = False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, *, schema_path, schema_id, init_ckpt, init_tok, out_dir,
              steps, n_train, log_path):
        if self.running():
            raise RuntimeError("a training job is already running")
        self.schema_id, self.out_dir, self.total = schema_id, out_dir, steps
        self.log, self.reloaded = Path(log_path), False
        cmd = [
            sys.executable, "-m", "tinyllm.cli", "train",
            "--schema", schema_path, "--init", init_ckpt, "--tok", init_tok,
            "--out", out_dir, "--train", str(n_train), "--val", "40",
            "--steps", str(steps), "--eval-every", str(steps), "--warmup", "30",
            "--device", "cpu",
        ]
        with open(self.log, "w") as f:
            self.proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)

    def status(self) -> dict:
        if self.proc is None:
            return {"state": "idle", "step": 0, "total": 0}
        code = self.proc.poll()
        step = 0
        if self.log and self.log.exists():
            found = _STEP.findall(self.log.read_text())
            if found:
                step = int(found[-1][0])
        state = "running" if code is None else ("done" if code == 0 else "failed")
        return {"state": state, "step": step, "total": self.total,
                "schema_id": self.schema_id, "out_dir": self.out_dir, "returncode": code}
