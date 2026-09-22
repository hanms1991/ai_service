# 第三方归属声明（Third-Party Notices）

本技能包中的以下文件移植自开源项目 **automotive-skills-suite**：

- 项目地址：https://github.com/jherrodthomas/automotive-skills-suite
- 许可证：MIT License
- 版权声明：Copyright (c) 2026 Jherrod Thomas

## 移植文件清单

| 文件 | 来源（原技能 hara-builder） | 改动 |
|---|---|---|
| `scripts/generate_hara.py` | `scripts/generate_hara.py` | 原样保留（仅依赖 openpyxl） |
| `references/malfunctions.md` | `references/malfunctions.md` | 原样保留 |
| `references/severity_ais.md` | `references/severity_ais.md` | 原样保留 |
| `references/exposure.md` | `references/exposure.md` | 原样保留 |
| `references/controllability.md` | `references/controllability.md` | 原样保留 |
| `references/asil_matrix.md` | `references/asil_matrix.md` | 原样保留 |
| `references/operating_environments.md` | `references/operating_environments.md` | 原样保留 |
| `references/fsc_handoff.md` | `references/fsc_handoff.md` | 原样保留 |
| `examples/sample_input_esc.json` | `examples/sample_input_esc.json` | 原样保留 |

## 天枢新增文件

- `hazard_analysis.yaml`：天枢技能契约（输入/输出/文档源/渲染器声明）
- `system_prompt.md`：中文工作流与输出纪律提示词

MIT 许可证全文：https://opensource.org/license/mit
