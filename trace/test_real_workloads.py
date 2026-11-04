"""Headless checks of workload guards, action logging, and failure cleanup."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SOURCE = Path(__file__).parent / "workloads/real.py"
spec = importlib.util.spec_from_file_location("real_workloads", SOURCE)
real = importlib.util.module_from_spec(spec)
spec.loader.exec_module(real)


class Workloads(unittest.TestCase):
    def query(self, name):
        return {"monitors": [{"name": "eDP-1"}], "activeworkspace": {"id": 3},
                "workspaces": [{"id": 3}, {"id": 7}], "clients": []}[name]

    def test_multi_display_rejected_before_mutation(self):
        with patch.object(sys, "argv", [str(SOURCE), "gtk-launch", "--check"]), \
             patch.object(real.shutil, "which", return_value="/bin/true"), \
             patch.object(real.subprocess, "check_output", return_value="none"), \
             patch.object(real, "hypr", return_value=[{"name": "eDP-1"}, {"name": "DP-1"}]), \
             patch.object(real, "focus") as focus:
            with self.assertRaises(SystemExit) as error:
                real.main()
            self.assertEqual(error.exception.code, 2)
            focus.assert_not_called()

    def test_launch_logs_actions_and_restores_workspace(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sys, "argv", [str(SOURCE), "gtk-launch", "--trials", "2", "--out", directory]), \
             patch.object(real.shutil, "which", return_value="/bin/true"), \
             patch.object(real.subprocess, "check_output", return_value="none"), \
             patch.object(real.subprocess, "Popen", return_value=MagicMock(pid=12345)) as popen, \
             patch.object(real, "hypr", side_effect=self.query), \
             patch.object(real, "focus") as focus, \
             patch.object(real, "stop") as stop, \
             patch.object(real.Run, "mapped"), patch.object(real.time, "sleep"):
            real.main()
            self.assertEqual([c.args[0] for c in focus.call_args_list], [8, 3])
            self.assertEqual(stop.call_count, 2)
            rows = [json.loads(line) for line in (Path(directory) / "events.jsonl").read_text().splitlines()]
            self.assertEqual(sum(r["event"] == "trial_end" for r in rows), 2)
            self.assertEqual(rows[-1]["event"], "complete")
            self.assertEqual([r["t_ns"] for r in rows], sorted(r["t_ns"] for r in rows))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_map_failure_cleans_process_and_restores_workspace(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sys, "argv", [str(SOURCE), "gtk-launch", "--out", directory]), \
             patch.object(real.shutil, "which", return_value="/bin/true"), \
             patch.object(real.subprocess, "check_output", return_value="none"), \
             patch.object(real.subprocess, "Popen", return_value=MagicMock(pid=12345)), \
             patch.object(real, "hypr", side_effect=self.query), \
             patch.object(real, "focus") as focus, \
             patch.object(real, "stop") as stop, \
             patch.object(real.Run, "mapped", side_effect=RuntimeError("map failed")), \
             patch.object(real.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "map failed"):
                real.main()
            self.assertEqual(focus.call_args.args, (3,))
            stop.assert_called_once()
            self.assertNotIn('"event": "complete"', (Path(directory) / "events.jsonl").read_text())

    def test_stop_escalates_for_unresponsive_group(self):
        process = MagicMock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("victim", 2), None, None, None]
        with patch.object(real.os, "killpg") as kill:
            real.stop(process)
        self.assertEqual([c.args[1] for c in kill.call_args_list],
                         [real.signal.SIGINT, real.signal.SIGTERM, real.signal.SIGKILL])

    def test_firefox_lost_focus_never_injects_input(self):
        def query(name):
            return {"title": "user's terminal"} if name == "activewindow" else self.query(name)

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sys, "argv", [str(SOURCE), "firefox-scroll", "--out", directory]), \
             patch.object(real.shutil, "which", return_value="/bin/true"), \
             patch.object(real.subprocess, "check_output", return_value="none"), \
             patch.object(real.subprocess, "Popen", return_value=MagicMock(pid=12345, poll=lambda: None)), \
             patch.object(real.subprocess, "run") as command, \
             patch.object(real, "hypr", side_effect=query), patch.object(real, "focus"), \
             patch.object(real, "stop") as stop, patch.object(real.Run, "mapped"), \
             patch.object(real.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "lost focus"):
                real.main()
            command.assert_not_called()
            stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
