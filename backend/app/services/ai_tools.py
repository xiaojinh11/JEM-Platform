"""Tool definitions and dispatch for the AI Agent.

These constants and the tool dispatcher are intentionally isolated so the
main ``ai.py`` service can remain readable.  Add new tools here.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# ── Tool definitions (OpenAI-compatible format) ─────────────────────────────────

TOOL_DEFS = [
    # ── Devices ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_devices",
            "description": "列出 / 检索设备。可按厂商、分组、角色过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {"type": "string", "description": "厂商，如 huawei_vrp / cisco_ios_xe"},
                    "group": {"type": "string", "description": "分组名"},
                    "role": {"type": "string", "description": "角色，如 access / aggregation / core"},
                    "keyword": {"type": "string", "description": "按名称或 IP 模糊搜索"},
                    "limit": {"type": "integer", "description": "最多返回多少台，默认 20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_config",
            "description": "获取设备最新运行配置（只读，长配置自动截断）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 ID"},
                    "max_chars": {"type": "integer", "description": "截断字符数，默认 4000"},
                },
                "required": ["device_id"],
            },
        },
    },
    # ── Topology / Graph (NEW) ────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_device_neighbors",
            "description": (
                "以一台设备为中心，沿图数据库走 N 跳，返回相邻的 Credential / Drift / Config 节点。"
                "用于回答「这台设备影响了哪些东西」「它的凭据、配置、漂移都是什么」这类拓扑问题。"
                "比起 list_devices + get_device_config，能一次性拿到关联信息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 ID"},
                    "hops": {"type": "integer", "description": "跳数，1..4，默认 1"},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["credential", "config", "document", "drift"]},
                        "description": "要拉取的邻居类型；默认全部",
                    },
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_affected_devices",
            "description": (
                "给定一条 drift 事件 id，返回它在图中能链到的所有实体。"
                "目前是 DRIFT_OF_DEVICE 反向 + 同设备最近其它 drift。"
                "用于「这条漂移影响谁」「同设备最近还有没有别的异常」。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "drift_id": {"type": "integer", "description": "Drift 事件 ID"},
                },
                "required": ["drift_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "topology_overview",
            "description": (
                "全网拓扑概览：按 role / vendor / group_name / model 把设备分桶统计。"
                "同时返回每个 vendor 的 drift 计数，便于 AI 看出「谁最近特别飘」。"
                "用户问「网络多大」「核心多少台」「哪家厂商最爱出问题」时优先用这个工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_by": {
                        "type": "string",
                        "enum": ["role", "vendor", "group_name", "model"],
                        "description": "分组维度，默认 role",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_vendor_model",
            "description": (
                "按 vendor / model / role / 关键词搜索活跃设备。"
                "用于「我们有几台 3850」「所有 huawei 设备」「某某型号的位置」这类基线检查前的查询。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {"type": "string", "description": "厂商名（精确匹配，不区分大小写）"},
                    "model": {"type": "string", "description": "型号名（模糊匹配）"},
                    "role": {"type": "string", "description": "设备角色，如 core / aggregation / access"},
                    "keyword": {"type": "string", "description": "按名称 / hostname / 序列号模糊搜索"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认 50"},
                },
                "required": [],
            },
        },
    },
    # ── Protocol / State (NEW) ─────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_interface_status",
            "description": (
                "从设备最新运行配置解析出所有接口的结构化信息："
                "名称、描述、IP/掩码、access/trunk vlan、是否 shutdown。"
                "用户问「X 设备的接口情况」「VLAN 10 在哪些端口」「哪些口 down 了」时用这个。"
                "注意：这是 *配置* 视角（shutdown = 配置上 shutdown），不是实时运行态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 ID"},
                    "name_pattern": {"type": "string", "description": "按接口名模糊过滤，例如 GigabitEthernet / Vlanif"},
                    "vlan_filter": {"type": "string", "description": "只看某 vlan 的接口"},
                    "include_shutdown": {"type": "boolean", "description": "是否包含 shutdown 接口，默认 true"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bgp_summary",
            "description": (
                "解析设备 BGP 配置：local AS、router-id、邻居列表（含 remote AS、描述、是否配了密码）。"
                "用于「BGP peer 列表」「BGP 安全审计（哪些 peer 没配密码）」「AS 拓扑分析」。"
                "注意：这是 *配置* 视角，反映运行 up/down 状态需要操作数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 ID"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ospf_neighbors",
            "description": (
                "解析设备 OSPF 配置：进程 ID、router-id、参与的网络和 area。"
                "用于「OSPF 区域拓扑」「哪些网段在 OSPF 里」「OSPF 进程有多少」。"
                "注意：这是 *配置* 视角，实际邻居状态需要 show ip ospf neighbor 操作数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "设备 ID"},
                },
                "required": ["device_id"],
            },
        },
    },
    # ── Config & Drift ────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "diff_revisions",
            "description": "对比两个版本之间的配置差异，返回 unified diff。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_revision_id": {"type": "integer"},
                    "to_revision_id": {"type": "integer"},
                },
                "required": ["from_revision_id", "to_revision_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_drifts",
            "description": "查看最近 N 条配置漂移事件，可按严重程度过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
                    "device_id": {"type": "integer", "description": "只看某一台设备"},
                    "limit": {"type": "integer", "description": "默认 10"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_drift_scan",
            "description": "立即触发全网配置漂移扫描（只读，不会修改设备）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acknowledge_drift",
            "description": "将某条漂移事件标记为已确认。",
            "parameters": {"type": "object", "properties": {"drift_id": {"type": "integer"}}, "required": ["drift_id"]},
        },
    },
    # ── Credentials ─────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_credentials",
            "description": "查看已保存的设备凭据（仅元数据，绝不返回明文密码）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "按名字搜索"},
                    "limit": {"type": "integer", "description": "默认 20"},
                },
                "required": [],
            },
        },
    },
    # ── Jobs ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": "查看最近的批量任务（备份、采集、配置下发），含状态摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "batch_read | batch_write | template_apply | backup"},
                    "status": {"type": "string", "description": "draft | queued | running | success | failed | partial"},
                    "limit": {"type": "integer", "description": "默认 10"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job",
            "description": "获取单个批量任务的详情，包括每个 step 的状态和错误。",
            "parameters": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_job",
            "description": (
                "创建一个批量任务。返回任务 ID。task_name 填写任务名称，"
                "kind 填写类型（backup=备份，batch_read=批量读取配置，template_apply=配置下发），"
                "device_ids 填写目标设备 ID 列表（可从 list_devices 获取），"
                "template_id 仅 template_apply 模式需要（可从 list_templates 获取）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string", "description": "任务名称"},
                    "kind": {"type": "string", "description": "任务类型：backup | batch_read | template_apply"},
                    "device_ids": {"type": "array", "items": {"type": "integer"}, "description": "目标设备 ID 列表"},
                    "template_id": {"type": "integer", "description": "模板 ID（template_apply 模式必须）"},
                    "dry_run": {"type": "boolean", "description": "是否仅模拟运行（不实际修改设备），默认 false"},
                },
                "required": ["task_name", "kind", "device_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_job",
            "description": "取消一个正在运行或排队的批量任务。",
            "parameters": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]},
        },
    },
    # ── Documents / Knowledge Base ──────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": (
                "列出知识库中的所有文档（标题、厂商、分类），不带搜索关键词。"
                "当用户问「有哪些文件」「手册库有什么」时优先使用此工具，而非 search_documents。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor": {"type": "string", "description": "按厂商过滤"},
                    "category": {"type": "string", "description": "按分类过滤"},
                    "limit": {"type": "integer", "description": "默认 50，最多 200"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "在知识库中全文检索关键字（Whoosh BM25 + 词干提取）。"
                "返回 Top-N 命中（带 score 字段）；score < 1.0 通常是噪音。"
                "**优先使用 rag_search 获得更好召回**——本工具是 BM25 单路召回，"
                "对口语化查询（如『怎么修邻居不通』）召回率较低。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键字"},
                    "limit": {"type": "integer", "description": "默认 5，最多 20"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": (
                "RAG 多路召回检索（**优先使用**）：先用 HyDE 让 LLM 生成假设文档，"
                "再用 BM25 检索该假设文档与原 query，最后 RRF 融合排序。"
                "对『怎么配 NTP』『邻居不通如何排查』这类口语化查询的召回率显著高于 search_documents。"
                "若返回 ``no_recall=True`` 表示知识库无相关内容，请不要编造答案。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户原始查询（不要先关键词化，让 HyDE 处理）"},
                    "limit": {"type": "integer", "description": "默认 5，最多 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document",
            "description": "获取单个文档的完整元数据（标题、厂商、分类、摘要）。如需下载原文件请告知用户去手册库页面手动下载。",
            "parameters": {"type": "object", "properties": {"doc_id": {"type": "integer", "description": "文档 ID"}}, "required": ["doc_id"]},
        },
    },
    # ── Templates ──────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_templates",
            "description": "列出所有配置模板（名称、描述、变量）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "按名称搜索"},
                    "limit": {"type": "integer", "description": "默认 20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_template",
            "description": "获取单个模板的完整源码（template_body 字段）。用于查看模板内容。",
            "parameters": {"type": "object", "properties": {"template_id": {"type": "integer"}}, "required": ["template_id"]},
        },
    },
    # ── Utility ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取服务器当前 UTC 时间。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_fetch",
            "description": "抓取一个 URL 的文本内容（GET only），用于阅读外部资料或厂商文档。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 http(s) URL"},
                    "max_chars": {"type": "integer", "description": "默认 5000"},
                },
                "required": ["url"],
            },
        },
    },
    # ── Scripts ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_scripts",
            "description": "查看用户上传的 Python 脚本清单（含描述、标签、最近运行状态）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "按名称或标签搜索"},
                    "limit": {"type": "integer", "description": "默认 20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_script",
            "description": "读取脚本源码，分析它接收什么参数、做什么事，返回结构化摘要。",
            "parameters": {"type": "object", "properties": {"script_id": {"type": "integer"}}, "required": ["script_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "执行用户上传的 Python 脚本（沙箱子进程，限时），返回 stdout / stderr / returncode。",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_id": {"type": "integer"},
                    "kwargs": {"type": "object", "description": "传给脚本 main(**kwargs) 的命名参数"},
                },
                "required": ["script_id"],
            },
        },
    },
    # ── Config plan proposal (AI chat → Dry-Run) ───────────────────
    # When the user asks "把 Core-SW1 的 100 段 VLAN 配上" or similar,
    # the LLM now has a *structured* way to hand back a would-be
    # ConfigPlan instead of embedding commands in markdown. The plan
    # rides on the chat response so the frontend can offer a single
    # "Dry-Run this AI plan" button — the same audit pipeline then
    # applies it. This is the only path that bridges the chat surface
    # into the job pipeline; the LLM must use this tool to propose.
    {
        "type": "function",
        "function": {
            "name": "propose_config_plan",
            "description": (
                "提出一份待执行的网络配置计划。返回给用户后会自动附 "
                "\"Dry-Run 该 AI 计划\"按钮。必须严格遵守：1) commands "
                "只能是字面量字符串数组，禁止包含变量、循环展开的运行 "
                "时产物；2) 不要把 reload/format/write erase 等高危单点"
                "操作写进 commands — 那种命令需要用户显式审批，必须单独"
                "确认；3) notes 字段用中文简要解释每段命令的目的。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "配置命令列表（每行一条，按下发顺序）",
                    },
                    "vendor": {
                        "type": "string",
                        "description": "目标厂商 (huawei_vrp / cisco_ios_xe / ruijie_rgos / h3c_comware)",
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "对命令的中文简要说明，会展示在 Dry-Run 摘要里",
                    },
                    "summary": {
                        "type": "string",
                        "description": "一句话描述本计划的目的，会作为按钮 tooltip",
                    },
                },
                "required": ["commands", "summary"],
            },
        },
    },
]
