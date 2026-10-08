"""ss_assignment_generate 技能的 postprocess 钩子：清洗 LLM 输出并校验分配完整性。

引擎约定入口：postprocess(ctx) -> dict | None
返回 {"structured": <cleaned>} 覆盖原输出；返回 None 保留原输出。
校验失败抛 ValueError，由引擎转为 SKILL_OUTPUT_INVALID（HTTP 422）。

校验规则（来自接口文档 5.6/5.7）：
  - structured 只含 assignments 字段
  - 每条 assignment 只含 fr_id/fr_revision_id/targets/note/unmatched_reason
  - 每个 target 只含 subsystem_id/rationale
  - fr_id + fr_revision_id 必须精确匹配输入 reference_data.sources 的 (artifact_id, revision_id)
  - subsystem_id 必须来自输入 reference_data.subsystems[].subsystem_id
  - 同一 assignment 内 subsystem_id 不得重复
  - targets 为空时 unmatched_reason 去空白后必须非空
  - assignments 1-100 条，不能为空
  - 覆盖完整性：每条输入 FR 必须且只能出现一次
"""
from __future__ import annotations

from typing import Any


def postprocess(ctx: Any) -> dict | None:
    """清洗并校验 SS 分配输出。"""
    structured = ctx.structured
    reference_data = getattr(ctx, "reference_data", None) or {}

    # 非后端直达场景（如 Planner 交互式调用）无 reference_data.sources，不做严格校验
    sources_in = reference_data.get("sources") or []
    subsystems_in = reference_data.get("subsystems") or []
    if not sources_in:
        return None

    # 构建合法 FR 对集合：{(artifact_id, revision_id)}
    valid_fr_pairs: set[tuple[str, str]] = set()
    for src in sources_in:
        if not isinstance(src, dict):
            continue
        artifact_id = src.get("artifact_id")
        revision_id = src.get("revision_id")
        if isinstance(artifact_id, str) and isinstance(revision_id, str):
            valid_fr_pairs.add((artifact_id, revision_id))

    # 构建合法 subsystem_id 集合
    valid_subsystem_ids: set[str] = set()
    for sub in subsystems_in:
        if isinstance(sub, dict):
            sid = sub.get("subsystem_id")
            if isinstance(sid, str):
                valid_subsystem_ids.add(sid)

    if not isinstance(structured, dict):
        raise ValueError("structured 必须是对象")

    # ── 剥顶层未知字段：只保留 assignments ──
    assignments = structured.get("assignments")
    if not isinstance(assignments, list):
        raise ValueError("assignments 必须是数组")

    if len(assignments) < 1:
        raise ValueError("assignments 不能为空（空数组不是合法成功结果）")

    if len(assignments) > 100:
        raise ValueError(f"assignments 超过 100 条上限，当前 {len(assignments)} 条")

    cleaned_assignments: list[dict[str, Any]] = []
    seen_fr_ids: set[str] = set()

    for i, asg in enumerate(assignments):
        if not isinstance(asg, dict):
            raise ValueError(f"assignments[{i}] 必须是对象")

        cleaned: dict[str, Any] = {}

        # ── fr_id：必须来自输入 sources[].artifact_id ──
        fr_id = asg.get("fr_id")
        if not isinstance(fr_id, str) or not fr_id.strip():
            raise ValueError(f"assignments[{i}].fr_id 不能为空")

        # ── fr_revision_id：必须与 fr_id 精确配对 ──
        fr_rev = asg.get("fr_revision_id")
        if not isinstance(fr_rev, str) or not fr_rev.strip():
            raise ValueError(f"assignments[{i}].fr_revision_id 不能为空")

        if (fr_id, fr_rev) not in valid_fr_pairs:
            raise ValueError(
                f"assignments[{i}] 的 fr_id={fr_id} fr_revision_id={fr_rev} "
                f"不在输入 reference_data.sources 中"
            )

        if fr_id in seen_fr_ids:
            raise ValueError(f"assignments[{i}].fr_id 重复：{fr_id}（每条 FR 只能出现一次）")
        seen_fr_ids.add(fr_id)

        cleaned["fr_id"] = fr_id
        cleaned["fr_revision_id"] = fr_rev

        # ── targets：0-100，剥未知字段，校验 subsystem 引用 ──
        targets = asg.get("targets")
        if targets is None:
            targets = []
        if not isinstance(targets, list):
            raise ValueError(f"assignments[{i}].targets 必须是数组")
        if len(targets) > 100:
            raise ValueError(f"assignments[{i}].targets 超过 100 个上限")

        seen_sub_ids: set[str] = set()
        cleaned_targets: list[dict[str, str]] = []
        for j, tgt in enumerate(targets):
            if not isinstance(tgt, dict):
                raise ValueError(f"assignments[{i}].targets[{j}] 必须是对象")

            sub_id = tgt.get("subsystem_id")
            if not isinstance(sub_id, str) or not sub_id.strip():
                raise ValueError(
                    f"assignments[{i}].targets[{j}].subsystem_id 不能为空"
                )
            if sub_id not in valid_subsystem_ids:
                raise ValueError(
                    f"assignments[{i}].targets[{j}].subsystem_id={sub_id} "
                    f"不在输入 reference_data.subsystems 中"
                )
            if sub_id in seen_sub_ids:
                raise ValueError(
                    f"assignments[{i}].targets[{j}].subsystem_id 重复：{sub_id}"
                )
            seen_sub_ids.add(sub_id)

            cleaned_tgt: dict[str, str] = {"subsystem_id": sub_id}

            rationale = tgt.get("rationale", "")
            if rationale is None:
                rationale = ""
            if not isinstance(rationale, str):
                raise ValueError(
                    f"assignments[{i}].targets[{j}].rationale 必须是字符串"
                )
            if len(rationale) > 20000:
                raise ValueError(
                    f"assignments[{i}].targets[{j}].rationale 超过 20000 字符"
                )
            cleaned_tgt["rationale"] = rationale

            cleaned_targets.append(cleaned_tgt)

        cleaned["targets"] = cleaned_targets

        # ── note：可选，≤20000，缺失默认 "" ──
        note = asg.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ValueError(f"assignments[{i}].note 必须是字符串")
        if len(note) > 20000:
            raise ValueError(f"assignments[{i}].note 超过 20000 字符")
        cleaned["note"] = note

        # ── unmatched_reason：targets 为空时必须非空 ──
        unmatched = asg.get("unmatched_reason", "")
        if unmatched is None:
            unmatched = ""
        if not isinstance(unmatched, str):
            raise ValueError(f"assignments[{i}].unmatched_reason 必须是字符串")
        if len(unmatched) > 20000:
            raise ValueError(f"assignments[{i}].unmatched_reason 超过 20000 字符")

        if len(cleaned_targets) == 0 and not unmatched.strip():
            raise ValueError(
                f"assignments[{i}].targets 为空时 unmatched_reason 不能为空"
            )
        cleaned["unmatched_reason"] = unmatched

        cleaned_assignments.append(cleaned)

    # ── 覆盖完整性：每条输入 FR 必须出现且只能出现一次 ──
    missing_frs = valid_fr_pairs - {
        (a["fr_id"], a["fr_revision_id"]) for a in cleaned_assignments
    }
    if missing_frs:
        missing_ids = [pair[0] for pair in missing_frs]
        raise ValueError(
            f"以下输入 FR 未在 assignments 中出现：{missing_ids}（每条 FR 必须覆盖且只能出现一次）"
        )

    return {"structured": {"assignments": cleaned_assignments}}
