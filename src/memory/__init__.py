"""记忆管理模块 — 滑动窗口 + 摘要 + 结构化事实

三层记忆架构:
1. 工作记忆 (MemoryManager.working_memory) — 最近 N 轮完整对话
2. 摘要记忆 (Summarizer) — 早期对话的压缩文本
3. 事实记忆 (FactStore) — 结构化关键信息，支持语义检索
"""
