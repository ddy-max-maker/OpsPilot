"""知识生命周期管理（D4，PRD §6 / CONTRACTS §2.9）。

状态机：draft → reviewed → published → superseded / retired（服务端强制）。
四眼原则：reviewed_by ≠ created_by；未发布/过期/退役知识对 Agent 检索不可见
（过滤发生在 RAG 检索层，见 app/domains/rag/retrieval.py）。
历史诊断报告 → 知识草稿：仅由人工触发创建 draft，绝不自动复核/发布。
"""
