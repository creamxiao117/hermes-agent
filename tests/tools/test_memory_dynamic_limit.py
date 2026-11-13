"""MemoryStore 动态体量约束（2026-11-13；中枢任务包 §1.5）。

## 为什么需要

旧口径 `memory_char_limit` 是**死上限**：记忆体量涨到顶就再也写不进 ⇒
逼着模型做无意义的压缩（用户真正想记的事实被挤掉）。

新口径「**基线 + 窗口增量**」：总量不再有死上限（基线随实测体量上移），
只卡「**快速膨胀**」—— 这正是中枢 `capability_reconcile` 月度体检的同款范式。

## 本文件钉死四件事

1. **默认逐字不变**：不配置 `memory_growth_limit` ⇒ 走旧固定上限（回归网要求）。
2. **有效上限 = max(基线, 当前实测) + 增量预算**。
3. **基线来源优先级**：中枢快照 > 构造参数 > 当前实测。
4. **user profile 不动态化**（体量小、语义稳定，动态化无收益）。

## 红绿证明

- 对**旧实现**（固定上限）必红（动态口径下上限会随实测上移）。
- 对**新实现**全绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.memory_tool_store import MemoryStore


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """把 memory 目录指到 tmp_path（不碰真实记忆）。"""
    import tools.memory_tool as mt

    monkeypatch.setattr(mt, "get_memory_dir", lambda: tmp_path / "memories")
    return tmp_path / "memories"


class TestDefaultBehaviorUnchanged:
    """默认口径必须逐字不变（这是回归网）。"""

    def test_no_growth_limit_keeps_fixed_limit(self, store_env):
        """不配置 memory_growth_limit ⇒ `_char_limit()` 返回固定值。"""
        s = MemoryStore(memory_char_limit=2200, user_char_limit=1375)
        assert s._char_limit("memory") == 2200
        assert s._char_limit("user") == 1375

    def test_growth_limit_zero_is_disabled(self, store_env):
        """`memory_growth_limit=0` 视为**未启用**（0 不当增量预算）。"""
        s = MemoryStore(memory_char_limit=2200, memory_growth_limit=0)
        assert s._char_limit("memory") == 2200


class TestDynamicLimit:
    """动态上限口径。"""

    def test_limit_is_baseline_plus_growth(self, store_env):
        """有效上限 = max(基线, 当前实测) + 增量预算。"""
        s = MemoryStore(memory_char_limit=2200, memory_growth_limit=500,
                        memory_char_baseline=1000)
        s.memory_entries = ["x" * 800]
        # 当前实测 800 < 基线 1000 ⇒ 上限 = 1000 + 500 = 1500
        assert s._char_limit("memory") == 1500

    def test_limit_tracks_current_when_above_baseline(self, store_env):
        """当前实测超过基线 ⇒ 上限随之上移（**无死上限**）。"""
        s = MemoryStore(memory_char_limit=2200, memory_growth_limit=500,
                        memory_char_baseline=1000)
        s.memory_entries = ["x" * 2000]
        # 当前 2000 > 基线 1000 ⇒ 上限 = 2000 + 500 = 2500
        assert s._char_limit("memory") == 2500

    def test_baseline_snapshot_wins_over_constructor(self, store_env):
        """中枢写的快照优先于构造参数（中枢是事实源，且免重启）。"""
        mem = store_env
        mem.mkdir(parents=True, exist_ok=True)
        (mem / ".memory_baseline.json").write_text(
            json.dumps({"char_count": 3000, "ts": 0}), encoding="utf-8"
        )
        s = MemoryStore(memory_char_limit=2200, memory_growth_limit=100,
                        memory_char_baseline=1000)
        s.memory_entries = ["x" * 10]
        # 基线 = 3000（快照）⇒ 上限 = 3000 + 100
        assert s._char_limit("memory") == 3100

    def test_user_profile_also_dynamic(self, store_env):
        """⚠ user profile **也**动态化（任务包 §2.3 实测告急：USER.md 只剩 31 字符）。

        固定上限会把正常写入挡死 ⇒ 动态化后同样只卡「快速膨胀」。
        """
        s = MemoryStore(memory_char_limit=2200, user_char_limit=1375,
                        memory_growth_limit=500, memory_char_baseline=1000)
        s.user_entries = ["y" * 3000]
        # 当前 3000 > 固定 1375 ⇒ 上限 = max(1000, 3000) + 500 = 3500
        assert s._char_limit("user") == 3500

    def test_user_profile_does_not_read_memory_snapshot(self, store_env):
        """user profile 的基线**不读** memory 快照（中枢不推 USER.md）。

        ⚠ 真实 bug（2026-11-13 实测发现）：基线若误用 memory 口径（14,186）
        ⇒ USER.md（1,469）的上限虚高到 14,386 ⇒ **完全失去约束**。
        """
        mem = store_env
        mem.mkdir(parents=True, exist_ok=True)
        (mem / ".memory_baseline.json").write_text(
            json.dumps({"char_count": 3000, "ts": 0}), encoding="utf-8"
        )
        s = MemoryStore(memory_char_limit=2200, user_char_limit=1375,
                        memory_growth_limit=100, memory_char_baseline=1000)
        s.user_entries = ["y" * 10]
        # 基线 = **当前实测** 10（不是快照 3000、也不是构造参数 1000）⇒ 上限 = 10 + 100
        assert s._char_limit("user") == 110

    def test_user_baseline_ignores_memory_char_baseline_param(self, store_env):
        """⚠ 构造参数 `memory_char_baseline` 是 **memory 口径** ⇒ 不得套到 user。

        真实 bug：14,186 套到 USER.md（1,469）⇒ 上限 14,386 ⇒ 约束全失。
        """
        s = MemoryStore(memory_char_limit=2200, user_char_limit=1375,
                        memory_growth_limit=2000, user_growth_limit=200,
                        memory_char_baseline=14186)
        s.user_entries = ["y" * 1469]
        # 上限 = max(当前实测 1469, ...) + 200 = 1669（**不是** 14,186 + 200）
        assert s._char_limit("user") == 1669

    def test_user_growth_limit_is_independent(self, store_env):
        """user profile 的增量预算**独立**（体量基数小，共用 memory 额度会太宽）。"""
        s = MemoryStore(memory_char_limit=2200, user_char_limit=1375,
                        memory_growth_limit=2000, user_growth_limit=200,
                        memory_char_baseline=1000)
        s.memory_entries = ["x" * 12000]
        s.user_entries = ["y" * 1400]
        assert s._char_limit("memory") == 12000 + 2000
        assert s._char_limit("user") == 1400 + 200

    def test_user_growth_limit_defaults_to_memory_limit(self, store_env):
        """未单独配 user_growth_limit ⇒ 回落到 memory_growth_limit（不额外要求配置）。"""
        s = MemoryStore(memory_char_limit=2200, memory_growth_limit=500)
        s.user_entries = ["y" * 100]
        assert s._char_limit("user") == 600


class TestBaselineSnapshot:
    """基线快照的落盘与读取。"""

    def test_snapshot_written_after_mutation(self, store_env):
        """写入后落一份快照（记录落盘后的真实体量）。"""
        mem = store_env
        mem.mkdir(parents=True, exist_ok=True)
        s = MemoryStore(memory_char_limit=2200, memory_growth_limit=500)
        s.add("memory", "用户偏好深色模式")
        snap_file = mem / ".memory_baseline.json"
        assert snap_file.is_file(), "写入后没有落快照"
        data = json.loads(snap_file.read_text(encoding="utf-8"))
        assert data["char_count"] > 0
        assert "ts" in data

    def test_snapshot_not_written_without_growth_limit(self, store_env):
        """未启用动态约束 ⇒ 不落快照（不产生多余文件）。"""
        mem = store_env
        mem.mkdir(parents=True, exist_ok=True)
        s = MemoryStore(memory_char_limit=2200)
        s.add("memory", "一条事实")
        assert not (mem / ".memory_baseline.json").exists()

    def test_corrupt_snapshot_falls_back_to_current(self, store_env):
        """快照损坏 ⇒ 回落到当前实测（不崩、不静默卡死）。"""
        mem = store_env
        mem.mkdir(parents=True, exist_ok=True)
        (mem / ".memory_baseline.json").write_text("{not json", encoding="utf-8")
        s = MemoryStore(memory_char_limit=2200, memory_growth_limit=100)
        s.memory_entries = ["x" * 50]
        # 回落到当前实测 50 ⇒ 上限 = 50 + 100
        assert s._char_limit("memory") == 150
