"""fr_generate 技能的 postprocess 钩子：清洗 LLM 输出并校验 UC 引用完整性。

引擎约定入口：postprocess(ctx) -> dict | None
返回 {"structured": <cleaned>} 覆盖原输出；返回 None 保留原输出。
校验失败抛 ValueError，由引擎转为 SKILL_OUTPUT_INVALID（HTTP 422）。

校验规则（来自接口文档 4.5/4.6）：
  - structured 只含 functional_requirements 字段
  - 每条 FR 只含 kind/description/acceptance_criteria/sources
  - kind 固定 "FR"
  - description 非空，≤20000 字符
  - acceptance_criteria ≤20000 字符，缺失默认 ""
  - sources 1-100 个，每项只含 uc_id/uc_revision_id
  - uc_id + uc_revision_id 必须精确匹配输入 reference_data.sources 的 (artifact_id, revision_id)
  - 同一 FR 内 uc_id 不得重复
  - functional_requirements 1-100 条，不能为空
"""
from __future__ import annotations

from typing import Any


def postprocess(ctx: Any) -> dict | None:
    """清洗并校验 FR 生成输出。"""
    structured = ctx.structured
    reference_data = getattr(ctx, "reference_data", None) or {}

    # 非后端直达场景（如 Planner 交互式调用）无 reference_data.sources，不做严格校验
    sources_in = reference_data.get("sources") or []
    if not sources_in:
        return None

    # 构建合法 UC 对集合：{(artifact_id, revision_id)}
    valid_uc_pairs: set[tuple[str, str]] = set()
    for src in sources_in:
        if not isinstance(src, dict):
            continue
        artifact_id = src.get("artifact_id")
        revision_id = src.get("revision_id")
        if isinstance(artifact_id, str) and isinstance(revision_id, str):
            valid_uc_pairs.add((artifact_id, revision_id))

    if not isinstance(structured, dict):
        raise ValueError("structured 必须是对象")

    # ── 剥顶层未知字段：只保留 functional_requirements ──
    frs = structured.get("functional_requirements")
    if not isinstance(frs, list):
        raise ValueError("functional_requirements 必须是数组")

    if len(frs) < 1:
        raise ValueError("functional_requirements 不能为空（空数组不是合法成功结果）")

    if len(frs) > 100:
        raise ValueError(
            f"functional_requirements 超过 100 条上限，当前 {len(frs)} 条"
        )

    cleaned_frs: list[dict[str, Any]] = []
    for i, fr in enumerate(frs):
        if not isinstance(fr, dict):
            raise ValueError(f"functional_requirements[{i}] 必须是对象")

        cleaned_fr: dict[str, Any] = {}

        # ── kind：固定 "FR" ──
        kind = fr.get("kind")
        if kind != "FR":
            raise ValueError(
                f"functional_requirements[{i}].kind 必须为 'FR'，当前为 {kind!r}"
            )
        cleaned_fr["kind"] = "FR"

        # ── description：非空，≤20000 ──
        desc = fr.get("description")
        if not isinstance(desc, str) or not desc.strip():
            raise ValueError(f"functional_requirements[{i}].description 不能为空")
        if len(desc) > 20000:
            raise ValueError(
                f"functional_requirements[{i}].description 超过 20000 字符"
            )
        cleaned_fr["description"] = desc

        # ── acceptance_criteria：可选，≤20000，缺失默认 ""，null → "" ──
        ac = fr.get("acceptance_criteria", "")
        if ac is None:
            ac = ""
        if not isinstance(ac, str):
            raise ValueError(
                f"functional_requirements[{i}].acceptance_criteria 必须是字符串"
            )
        if len(ac) > 20000:
            raise ValueError(
                f"functional_requirements[{i}].acceptance_criteria 超过 20000 字符"
            )
        cleaned_fr["acceptance_criteria"] = ac

        # ── sources：1-100，剥未知字段，校验引用合法性 ──
        srcs = fr.get("sources")
        if not isinstance(srcs, list) or len(srcs) < 1:
            raise ValueError(
                f"functional_requirements[{i}].sources 至少需要 1 个来源"
            )
        if len(srcs) > 100:
            raise ValueError(
                f"functional_requirements[{i}].sources 超过 100 个上限"
            )

        seen_uc_ids: set[str] = set()
        cleaned_srcs: list[dict[str, str]] = []
        for j, src in enumerate(srcs):
            if not isinstance(src, dict):
                raise ValueError(
                    f"functional_requirements[{i}].sources[{j}] 必须是对象"
                )

            uc_id = src.get("uc_id")
            uc_rev = src.get("uc_revision_id")

            if not isinstance(uc_id, str) or not uc_id.strip():
                raise ValueError(
                    f"functional_requirements[{i}].sources[{j}].uc_id 不能为空"
                )
            if not isinstance(uc_rev, str) or not uc_rev.strip():
                raise ValueError(
                    f"functional_requirements[{i}].sources[{j}].uc_revision_id 不能为空"
                )

            if uc_id in seen_uc_ids:
                raise ValueError(
                    f"functional_requirements[{i}].sources[{j}].uc_id 重复：{uc_id}"
                )
            seen_uc_ids.add(uc_id)

            if (uc_id, uc_rev) not in valid_uc_pairs:
                raise ValueError(
                    f"functional_requirements[{i}].sources[{j}] 的 "
                    f"uc_id={uc_id} uc_revision_id={uc_rev} "
                    f"不在输入 reference_data.sources 中"
                )

            cleaned_srcs.append({"uc_id": uc_id, "uc_revision_id": uc_rev})

        cleaned_fr["sources"] = cleaned_srcs
        cleaned_frs.append(cleaned_fr)

    return {"structured": {"functional_requirements": cleaned_frs}}
