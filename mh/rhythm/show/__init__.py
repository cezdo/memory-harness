"""展示自己的窗口（runbook N3，用户 2026-11-01）：只读网页“此刻的我”，打开时现编。
collect 收条目，render 排页，serve 常驻服务（mh rhythm --show，memory-harness-show）。"""
from .collect import collect
from .render import render


def page(now=None):
    return render(collect(now), now)
