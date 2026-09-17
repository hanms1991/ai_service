"""场景码 → (agent, skill, response_format, timeout) 解析。

启动时加载 agents/configs/scenes.yaml 并与能力注册表交叉校验：
  - scene.agent 必须存在于注册表；
  - scene.skill 非空时必须存在于该 agent 的技能列表；
  - response_format=json 时对应技能 output.format 必须为 json；
  - 违规在启动期报错，不带到运行时。

运行时通过 resolve(scene) 解析为内部 (agent, skill, response_format, timeout)。
后端只传 scene，不感知 agent/skill 的命名重构。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agents.capability_registry import CONFIGS_DIR, get_registry, load_agent_config


@dataclass
class SceneBinding:
    """解析后的场景码绑定。"""
    scene: str
    description: str
    agent: str
    skill: str          # 空字符串表示走 Planner 智能编排
    response_format: str  # text | json
    timeout_seconds: int


class SceneResolver:
    """场景码解析器（启动期加载 + 启动期交叉校验）。

    用法：
        resolver = SceneResolver()
        resolver.load_and_validate()           # 启动时调用一次
        binding = resolver.resolve("ONE_CLICK_EXPAND")
    """

    def __init__(self, scenes_path: Path | None = None):
        self.scenes_path = scenes_path or (CONFIGS_DIR / "scenes.yaml")
        self._scenes: dict[str, dict[str, Any]] = {}
        self._bindings: dict[str, SceneBinding] = {}

    def load_and_validate(self) -> None:
        """加载 scenes.yaml 并与能力注册表做交叉校验。

        校验失败抛 ValueError，由 lifespan 捕获后阻断启动。
        """
        if not self.scenes_path.exists():
            raise FileNotFoundError(
                f"scenes.yaml 不存在：{self.scenes_path}，请按设计文档 9A 节创建"
            )

        with self.scenes_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        self._scenes = data.get("scenes") or {}
        if not self._scenes:
            # 空配置允许通过（一期可能没启用任何场景码），只打 warning
            return

        registry = get_registry()
        agents_in_registry = registry.get("agents", {})

        for scene_name, cfg in self._scenes.items():
            agent_name = cfg.get("agent")
            if not agent_name:
                raise ValueError(
                    f"场景码 {scene_name} 缺少 agent 字段"
                )
            if agent_name not in agents_in_registry:
                raise ValueError(
                    f"场景码 {scene_name}.agent={agent_name!r} 不在能力注册表，"
                    f"可用：{', '.join(agents_in_registry) or '（空）'}"
                )

            skill_name = cfg.get("skill", "")
            if skill_name:
                agent_skills = agents_in_registry[agent_name].get("skills", {})
                if skill_name not in agent_skills:
                    raise ValueError(
                        f"场景码 {scene_name}.skill={skill_name!r} 不在 "
                        f"{agent_name} 的技能列表，可用：{', '.join(agent_skills) or '（空）'}"
                    )

                # response_format=json 时要求技能 output.format=json
                response_format = cfg.get("response_format", "text")
                if response_format == "json":
                    skill_output = agent_skills[skill_name].get("output", {}) or {}
                    if skill_output.get("format") != "json":
                        raise ValueError(
                            f"场景码 {scene_name} 声明 response_format=json，"
                            f"但技能 {skill_name} 的 output.format={skill_output.get('format')!r}，"
                            f"二者冲突"
                        )

            # 落 binding
            self._bindings[scene_name] = SceneBinding(
                scene=scene_name,
                description=cfg.get("description", ""),
                agent=agent_name,
                skill=skill_name or "",
                response_format=cfg.get("response_format", "text"),
                timeout_seconds=cfg.get("timeout_seconds", 60),
            )

    def resolve(self, scene: str) -> SceneBinding:
        """运行时解析场景码为内部 (agent, skill, ...)。

        Raises:
            KeyError: 场景码未在 scenes.yaml 声明
        """
        binding = self._bindings.get(scene)
        if binding is None:
            raise KeyError(
                f"场景码 {scene!r} 不存在于 scenes.yaml，"
                f"可用：{', '.join(self._bindings) or '（空）'}"
            )
        return binding

    def list_scenes(self) -> list[SceneBinding]:
        """列出全部场景码（供 /capabilities 接口对外返回）。"""
        return list(self._bindings.values())

    def has_scene(self, scene: str) -> bool:
        return scene in self._bindings


# 全局单例（lifespan 启动时初始化）
_resolver: SceneResolver | None = None


def init_scene_resolver() -> SceneResolver:
    """启动时初始化全局 SceneResolver 并做交叉校验。"""
    global _resolver
    _resolver = SceneResolver()
    _resolver.load_and_validate()
    return _resolver


def get_scene_resolver() -> SceneResolver:
    """获取全局 SceneResolver 单例。"""
    if _resolver is None:
        raise RuntimeError("SceneResolver 尚未初始化，请在 lifespan 中调用 init_scene_resolver()")
    return _resolver
