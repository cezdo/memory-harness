import unittest
from datetime import datetime, timedelta, timezone

from .collect import ago, runbook_state, tunnel
from .measure import linux, mac, windows
from .render import render, row_spans


class Show(unittest.TestCase):
    def test_row_spans(self):
        self.assertEqual(row_spans(2, 6), [6, 6])
        self.assertEqual(row_spans(3, 6), [6, 6, 12])          # 最后一行只剩一块，占满一行
        self.assertEqual(row_spans(5, 4), [4, 4, 4, 6, 6])
        self.assertEqual(row_spans(0, 4), [])

    def test_tunnel(self):
        self.assertEqual(tunnel(False, "")[0], "bad")
        self.assertEqual(tunnel(True, "426"), ("ok", "通，协议端在"))
        self.assertEqual(tunnel(True, "000")[0], "warn")

    def test_runbook_state(self):
        self.assertEqual(runbook_state("做了一半。下一步：拿本地模型试。"), ("next", "拿本地模型试", None))
        self.assertEqual(runbook_state("等 C 能跑再展开"), ("wait", None, "等 C 能跑再展开"))
        self.assertEqual(runbook_state("串联等待只剩 observe")[0], "open")   # “等待”不算在等

    def test_ago(self):
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        self.assertEqual(ago(now - timedelta(minutes=5), now), "5 分钟前")
        self.assertEqual(ago(now - timedelta(hours=3), now), "3 小时前")

    def test_render_minimal(self):
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        items = [{"kind": "服务", "title": "observe", "state": "ok", "note": "近 6 小时没出错"},
                 {"kind": "事", "title": "G4 节律的核心", "thing": "节律", "state": "next", "next": "试", "wait": None,
                  "body": "x", "size": 1, "time": (now - timedelta(days=2)).isoformat()}]
        html = render(items, now, cfg={})
        self.assertIn("此刻的我", html)
        self.assertIn("上次动是 2 天前", html)
        self.assertNotIn("没动", html.replace("上次动", ""))


    def test_measure_parsers(self):
        lx = linux("8\n3.61 3.60 3.55 1/578 1\n90000.5 1000.0\ncpu  100 0 100 700 100 0 0 0 0 0\ncpu  150 0 150 750 150 0 0 0 0 0\n"
                   "MemTotal:        8000 kB\nMemAvailable:    2000 kB\nMounted on 1B-blocks Used\n/ 1000 400\n/home 2000 1000\n")
        self.assertEqual((lx["cores"], lx["cpu"], lx["mem"]), (8, 50.0, [6000 * 1024, 8000 * 1024]))   # 忙 100 / 共 200
        self.assertEqual(lx["disks"], [["/", 1000, 400], ["/home", 2000, 1000]])
        mc = mac("18\n38654705664\n{ sec = 1793494122, usec = 1 } Sun\n{ 0.15 0.43 0.58 }\nSystem-wide memory free percentage: 25%\n"
                 "/dev/disk3s5 100 40 60 40% /System/Volumes/Data\n\"Device Utilization %\"=7\n\"In use system memory\"=1024\n"
                 "CPU usage: 1.0% user, 2.0% sys, 97.0% idle\n")
        self.assertEqual((mc["cpu"], mc["gpu"], mc["mem"][0], mc["disks"]), (3.0, 7, 38654705664 * 3 // 4, [["/", 102400, 40960]]))
        wn = windows('#< CLIXML\n{"mem":[1,2],"cpu":6,"vram":5,"boot":"2026-10-29T21:21:11.0604980Z","disks":["C:",100,40],"cores":16,"gpu":56.61}')
        self.assertEqual((wn["disks"], wn["gpu"]), ([["C:", 100, 40]], 56.6))    # 只有一块盘时 PowerShell 不套外层数组

    def test_render_machines(self):
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        items = [{"kind": "机器", "title": "workstation", "state": "ok", "at": now.isoformat(), "boot": (now - timedelta(days=2)).isoformat(),
                  "local": True, "cpu": 6, "cores": 16, "mem": [2**33, 2**35], "gpu": 56.6, "vram": 6 * 2**30,
                  "disks": [["C:", 2**41, 2**40]]},
                 {"kind": "机器", "title": "VPS", "state": "bad", "at": (now - timedelta(hours=2)).isoformat(), "err": "量不到：超时"}]
        html = render(items, now, cfg={})
        self.assertIn("开机 2 天", html)
        self.assertIn("机器 1/2 在线", html)
        self.assertIn("上次量是 2 小时前", html)
        self.assertNotIn("C:", html)                       # 本机的磁盘在“重启后看这里”，不重复列


if __name__ == "__main__":
    unittest.main()
