"""Short planning-lock adapter for PlotPilot native story planning."""
from __future__ import annotations

from hashlib import sha256
from typing import Any, Optional


def build_planning_lock(payload: dict[str, Any], *, purpose: str) -> dict[str, Any]:
    bible_context = payload.get("bible_context") if isinstance(payload.get("bible_context"), dict) else {}
    premise = str(payload.get("premise") or payload.get("novel_premise") or "").strip()
    title = str(payload.get("novel_title") or payload.get("title") or "").strip()
    genre = str(payload.get("genre") or "").strip()
    world_preset = str(payload.get("world_preset") or "").strip()
    style_hint = str(payload.get("style_hint") or "").strip()
    try:
        target_chapters = int(payload.get("target_chapters") or 0)
    except (TypeError, ValueError):
        target_chapters = 0

    if not style_hint and bible_context:
        style_hint = str(bible_context.get("style_hint") or "").strip()
    bible_counts = {
        "characters": len(bible_context.get("characters") or []) if isinstance(bible_context.get("characters"), list) else 0,
        "world_settings": len(bible_context.get("world_settings") or []) if isinstance(bible_context.get("world_settings"), list) else 0,
        "locations": len(bible_context.get("locations") or []) if isinstance(bible_context.get("locations"), list) else 0,
        "timeline_notes": len(bible_context.get("timeline_notes") or []) if isinstance(bible_context.get("timeline_notes"), list) else 0,
    }
    bible_empty = not any(bible_counts.values())
    constraints = []
    if premise:
        constraints.append("必须以用户 premise 作为宏观规划硬输入，不得改写题材、主线承诺或核心冲突。")
    if genre or world_preset:
        constraints.append("类型/世界观基调优先级高于通用网文模板；不能用跨题材固定开局或升级模板替代。")
    if bible_empty:
        constraints.append("Bible 暂无详细资料时，只能围绕 premise 合理补全，不能把补全内容当作已确认事实。")
    if style_hint:
        constraints.append("文风提示只约束表达方式，不得反向改变题材或人物关系。")
    return {
        "purpose": purpose,
        "novel_title": title[:200],
        "premise": premise[:1200],
        "genre": genre[:120],
        "world_preset": world_preset[:200],
        "target_chapters": target_chapters,
        "style_hint": style_hint[:500],
        "bible_counts": bible_counts,
        "bible_empty": bible_empty,
        "constraints": constraints[:6],
        "has_lock": bool(premise or genre or world_preset or style_hint),
    }


def planning_payload_with_worldline_defaults(payload: dict[str, Any], worldline: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload or {})
    for key in ("premise", "genre", "world_preset", "target_chapters", "style_hint"):
        if not merged.get(key) and isinstance(worldline, dict) and worldline.get(key):
            merged[key] = worldline.get(key)
    if not merged.get("novel_title") and not merged.get("title") and isinstance(worldline, dict) and worldline.get("title"):
        merged["novel_title"] = worldline.get("title")
    return merged


def build_planning_alignment(planning_lock: dict[str, Any], *, evidence: dict[str, Any], rendered_chars: int) -> dict[str, Any]:
    return {
        "source": "before_story_planning",
        "purpose": planning_lock.get("purpose") or "story_planning",
        "premise_received": bool(str(planning_lock.get("premise") or "").strip()),
        "planning_lock_generated": bool(planning_lock.get("constraints")),
        "bible_empty_fallback": bool(planning_lock.get("bible_empty")),
        "prehistory_available": bool(evidence),
        "rendered_chars": rendered_chars,
        "title": planning_lock.get("novel_title") or "",
        "genre": planning_lock.get("genre") or "",
        "world_preset": planning_lock.get("world_preset") or "",
        "target_chapters": planning_lock.get("target_chapters") or 0,
        "bible_counts": dict(planning_lock.get("bible_counts") or {}),
    }


def render_planning_adapter_context(
    planning_lock: dict[str, Any],
    evidence: dict[str, Any],
    *,
    style_adapter: Optional[dict[str, Any]] = None,
) -> str:
    parts = []
    if planning_lock.get("has_lock"):
        parts.append(_render_planning_lock(planning_lock))
    if evidence:
        parts.append(_render_story_planning_evidence(evidence, style_adapter=style_adapter))
    return "\n\n".join(part for part in parts if part.strip())


def build_prehistory_worldline(
    *,
    novel_id: str,
    title: str,
    premise: str,
    genre: str,
    world_preset: str,
    style_hint: str,
    target_chapters: Optional[int],
    length_tier: str,
    at: str,
) -> dict[str, Any]:
    profile = _select_worldline_profile(genre, world_preset, premise, target_chapters, length_tier)
    axes = _infer_story_axes(genre, world_preset, premise)
    style_adapter = _build_style_adapter(
        title=title,
        premise=premise,
        genre=genre,
        world_preset=world_preset,
        style_hint=style_hint,
    )
    forces = _build_world_forces(axes, profile)
    eras = _build_prehistory_eras(profile, axes, forces, title)
    seeds = _build_prehistory_foreshadow_seeds(profile, axes, forces)
    guidance = _build_prehistory_guidance(profile, axes)
    return {
        "schema_version": 1,
        "novel_id": novel_id,
        "title": title,
        "premise": premise[:1200],
        "genre": genre[:120],
        "world_preset": world_preset[:200],
        "target_chapters": target_chapters or 0,
        "style_hint": style_hint[:500],
        "source": "deterministic_prehistory_generator",
        "created_at": at,
        "input_digest": _hash_text("|".join([title, genre, world_preset, premise, str(target_chapters or ""), length_tier])),
        "depth": {
            "tier": profile["tier"],
            "label": profile["label"],
            "horizon_years": profile["horizon_years"],
            "era_count": profile["era_count"],
            "detail_level": profile["detail_level"],
            "reason": profile["reason"],
        },
        "story_axes": axes,
        "style_adapter": style_adapter,
        "eras": eras,
        "forces": forces,
        "foreshadow_seeds": seeds,
        "planning_guidance": guidance,
    }


def build_runtime_style_adapter(worldline: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    stored = worldline.get("style_adapter") if isinstance(worldline.get("style_adapter"), dict) else {}
    style_hint = _extract_runtime_style_hint(payload)
    if not style_hint:
        return stored or _build_style_adapter()
    runtime = _build_style_adapter(
        title=str(worldline.get("title") or ""),
        premise=str(payload.get("premise") or payload.get("novel_premise") or ""),
        genre=str(payload.get("genre") or ""),
        world_preset=str(payload.get("world_preset") or ""),
        style_hint=style_hint,
    )
    runtime["base_detected_style_tags"] = stored.get("detected_style_tags") or []
    runtime["style_source"] = "runtime_payload"
    return runtime


def _render_planning_lock(planning_lock: dict[str, Any]) -> str:
    lines = ["【Evolution 规划硬约束】"]
    if planning_lock.get("novel_title"):
        lines.append(f"- 标题：{planning_lock['novel_title']}")
    if planning_lock.get("genre"):
        lines.append(f"- 类型/赛道：{planning_lock['genre']}")
    if planning_lock.get("world_preset"):
        lines.append(f"- 世界观基调：{planning_lock['world_preset']}")
    if planning_lock.get("target_chapters"):
        lines.append(f"- 目标章数：{planning_lock['target_chapters']}")
    if planning_lock.get("premise"):
        lines.append(f"- 用户 premise：{planning_lock['premise']}")
    if planning_lock.get("style_hint"):
        lines.append(f"- 文风提示：{planning_lock['style_hint']}")
    lines.append("【规划锁】")
    for item in planning_lock.get("constraints") or []:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _render_story_planning_evidence(evidence: dict[str, Any], *, style_adapter: Optional[dict[str, Any]] = None) -> str:
    worldline = evidence.get("worldline") or {}
    depth = worldline.get("depth") or {}
    style_adapter = style_adapter or worldline.get("style_adapter") or {}
    lines = [
        f"前史深度：{depth.get('label', '未定')}；跨度：约{depth.get('horizon_years', 0)}年；原因：{depth.get('reason', '')}",
    ]
    if style_adapter:
        axes = style_adapter.get("style_axes") or {}
        lines.append("【文风适配协议】")
        lines.append(f"- 当前文风标签：{'、'.join(_as_strings(style_adapter.get('detected_style_tags'))) or '自定义/未指定'}；前史条目只作为语义蓝图，不能原样写进正文。")
        if style_adapter.get("requested_style"):
            lines.append(f"- 用户/Bible文风提示：{str(style_adapter.get('requested_style'))[:240]}")
        for item in style_adapter.get("adaptation_contract") or []:
            lines.append(f"- {item}")
        if axes:
            lines.append(
                "- 转译方式："
                f"措辞={axes.get('diction', '')}；"
                f"节奏={axes.get('sentence_rhythm', '')}；"
                f"意象={axes.get('imagery', '')}；"
                f"揭示={axes.get('revelation', '')}"
            )
    if evidence.get("eras"):
        lines.append("【故事开始前的世界线】")
        for era in evidence["eras"]:
            lines.append(f"- {era.get('time_label')}｜{era.get('name')}：{era.get('summary')} 因果作用：{era.get('causal_effect')}")
    if evidence.get("forces"):
        lines.append("【势力/制度因果】")
        for force in evidence["forces"]:
            lines.append(f"- {force.get('name')}：欲望={force.get('desire')}；弱点={force.get('weakness')}")
    if evidence.get("foreshadow_seeds"):
        lines.append("【可用于大纲与伏笔的种子】")
        for seed in evidence["foreshadow_seeds"]:
            lines.append(f"- {seed.get('axis')}：{seed.get('planting_form')} 真相={seed.get('true_meaning')}")
    if evidence.get("planning_guidance"):
        lines.append("【使用约束】")
        lines.extend(f"- {item}" for item in evidence["planning_guidance"])
    return "\n".join(line for line in lines if str(line).strip())


def _as_strings(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(item) for item in items if str(item or "").strip()]


def _select_worldline_profile(
    genre: str,
    world_preset: str,
    premise: str,
    target_chapters: Optional[int],
    length_tier: str,
) -> dict[str, Any]:
    text = " ".join([genre, world_preset, premise, length_tier]).lower()
    epic_terms = ["玄幻", "修仙", "仙侠", "奇幻", "史诗", "神话", "王朝", "帝国", "科幻", "星际", "宇宙", "克苏鲁", "文明"]
    complex_terms = ["悬疑", "推理", "权谋", "谍战", "战争", "末世", "赛博", "犯罪", "宫斗", "阴谋", "群像", "历史"]
    intimate_terms = ["都市", "校园", "日常", "恋爱", "青春", "职场", "家庭", "轻喜", "现代"]
    target = target_chapters or 100
    if length_tier == "epic" or target >= 500 or any(term in text for term in epic_terms):
        return {
            "tier": "epic",
            "label": "宏大长线",
            "horizon_years": 3000 if target < 1000 else 10000,
            "era_count": 6 if target < 1000 else 7,
            "detail_level": "high",
            "reason": "题材或篇幅需要跨文明/跨时代因果，前史必须提供制度、灾难与禁忌的长线来源。",
        }
    if target >= 200 or any(term in text for term in complex_terms):
        return {
            "tier": "complex",
            "label": "复杂因果",
            "horizon_years": 180,
            "era_count": 5,
            "detail_level": "medium_high",
            "reason": "题材强调阴谋、制度或多方博弈，需要至少数代人的秘密、旧案和势力传承。",
        }
    if any(term in text for term in intimate_terms):
        return {
            "tier": "intimate",
            "label": "近现代关系线",
            "horizon_years": 12,
            "era_count": 3,
            "detail_level": "focused",
            "reason": "题材更重人物关系和当代生活，前史以近年创伤、家庭/学校/职场制度和关系源头为主。",
        }
    return {
        "tier": "standard",
        "label": "标准长篇",
        "horizon_years": 60,
        "era_count": 4,
        "detail_level": "medium",
        "reason": "默认按中篇商业叙事处理，保留一代以上因果和开篇前夜的可用伏笔。",
    }


def _infer_story_axes(genre: str, world_preset: str, premise: str) -> list[str]:
    text = " ".join([genre, world_preset, premise])
    candidates = [
        ("权力秩序", ["权", "王", "贵族", "组织", "公司", "帝国", "宗门", "学校"]),
        ("禁忌知识", ["禁", "秘", "真相", "档案", "旧案", "研究", "知识", "黑箱"]),
        ("资源争夺", ["资源", "灵气", "矿", "能源", "钥匙", "遗产", "名额", "土地"]),
        ("身份伪装", ["伪装", "身份", "替身", "大小姐", "卧底", "假", "面具"]),
        ("情感依赖", ["依赖", "爱", "亲吻", "拥抱", "家人", "青梅", "搭档", "守护"]),
        ("异常觉醒", ["觉醒", "异能", "异常", "系统", "天赋", "魔法", "污染", "变异"]),
        ("灾难余波", ["灾", "战争", "崩溃", "末世", "瘟疫", "事故", "袭击", "毁灭"]),
    ]
    axes = [name for name, terms in candidates if any(term in text for term in terms)]
    if not axes:
        axes = ["权力秩序", "人物欲望", "隐藏真相"]
    elif len(axes) == 1:
        axes.append("人物欲望")
    return axes[:4]


def _build_world_forces(axes: list[str], profile: dict[str, Any]) -> list[dict[str, str]]:
    forces = []
    for index, axis in enumerate(axes, start=1):
        force_type = "institution" if axis in {"权力秩序", "身份伪装"} else "pressure"
        forces.append(
            {
                "force_id": f"force_{index}",
                "name": f"{axis}的既得利益者",
                "type": force_type,
                "desire": f"维持{axis}带来的优势，不允许开篇主线轻易揭开根因。",
                "weakness": f"{axis}的历史断层或见不得光的交换条件。",
                "planning_use": "可作为主线阻力、阶段反派或伏笔回收对象。",
            }
        )
    if profile["tier"] in {"epic", "complex"}:
        forces.append(
            {
                "force_id": "force_legacy",
                "name": "旧时代残留机制",
                "type": "legacy_system",
                "desire": "继续按旧规则筛选幸存者、继承人或真相持有者。",
                "weakness": "只要有人理解旧时代的代价，就能绕开表层秩序。",
                "planning_use": "用于解释远古遗迹、秘密机构、旧案卷宗和终局反转。",
            }
        )
    return forces


def _build_prehistory_eras(
    profile: dict[str, Any],
    axes: list[str],
    forces: list[dict[str, str]],
    title: str,
) -> list[dict[str, Any]]:
    names = ["根源期", "制度成形期", "第一次创伤期", "秩序粉饰期", "暗流积累期", "开篇前夜", "未公开余波期"]
    horizon = int(profile["horizon_years"])
    count = int(profile["era_count"])
    span = max(horizon // count, 1)
    eras = []
    for index in range(count):
        starts = horizon - span * index
        ends = max(horizon - span * (index + 1), 0)
        axis = axes[index % len(axes)]
        force = forces[index % len(forces)]
        if index == count - 1:
            time_label = "开篇前1年-第1章前"
        else:
            time_label = f"开篇前约{starts}-{ends}年"
        eras.append(
            {
                "era_id": f"pre_{index + 1}",
                "name": names[index],
                "time_label": time_label,
                "summary": _era_summary(names[index], axis, force.get("name", ""), title),
                "causal_effect": f"把{axis}转化为开篇可见的压力，使主角面对的不是偶然麻烦，而是历史长期积累后的爆点。",
                "planning_hooks": [
                    f"用一件看似日常的小物/制度痕迹暗示{_name_or_axis(names[index], axis)}。",
                    f"让{force.get('name')}的行动暴露一条旧因果，但暂不解释全部真相。",
                ],
            }
        )
    return eras


def _era_summary(era_name: str, axis: str, force_name: str, title: str) -> str:
    subject = title or "本故事"
    if era_name == "根源期":
        return f"{subject}的核心矛盾在{axis}上首次成形，{force_name}掌握了最初的解释权。"
    if era_name == "制度成形期":
        return f"围绕{axis}形成稳定制度，公开规则保护秩序，隐藏规则保护少数人的收益。"
    if era_name == "第一次创伤期":
        return f"{axis}引发无法公开的事故、背叛或牺牲，成为后续人物命运的隐性债务。"
    if era_name == "秩序粉饰期":
        return f"旧创伤被改写成合理历史，幸存者、受益者和失语者被安排到不同位置。"
    if era_name == "暗流积累期":
        return f"被压住的证据和欲望重新靠近开篇人物，冲突开始从背景走向台前。"
    return f"开篇前夜，各方围绕{axis}完成最后一次布置，主角即将撞上这条历史暗线。"


def _name_or_axis(name: str, axis: str) -> str:
    return axis if name == "开篇前夜" else f"{name}的{axis}"


def _build_prehistory_foreshadow_seeds(
    profile: dict[str, Any],
    axes: list[str],
    forces: list[dict[str, str]],
) -> list[dict[str, Any]]:
    seeds = []
    for index, axis in enumerate(axes, start=1):
        force = forces[(index - 1) % len(forces)]
        seeds.append(
            {
                "seed_id": f"seed_{index}",
                "axis": axis,
                "planting_form": f"开篇用一句异常称呼、一份残缺记录或一次不合常理的回避埋下{axis}。",
                "surface_meaning": "读者初看只会认为这是世界观质感或人物习惯。",
                "true_meaning": f"它指向{force.get('name')}在前史中留下的债务。",
                "recommended_payoff": "中后期当主角掌握证据或付出代价后再解释完整因果。",
            }
        )
    if profile["tier"] in {"epic", "complex"}:
        seeds.append(
            {
                "seed_id": "seed_epoch_lie",
                "axis": "历史谎言",
                "planting_form": "让官方年表、家族传说或组织记录出现一个无法同时成立的日期。",
                "surface_meaning": "像是资料误差。",
                "true_meaning": "旧时代被人为截断，某个关键事件发生时间被整体改写。",
                "recommended_payoff": "用于卷末或部末反转，推动主线从个人冲突升级为世界结构冲突。",
            }
        )
    return seeds


def _build_prehistory_guidance(profile: dict[str, Any], axes: list[str]) -> list[str]:
    guidance = [
        "前史只提供因果压力，不替代正文选择；规划时应把它转化为角色目标、误判、代价和伏笔。",
        f"当前前史深度为{profile['label']}：大纲中至少选择一条前史因果进入第一卷，一条保留到中后期回收。",
        f"优先围绕{axes[0]}设计开篇钩子，让读者先看到结果，再逐步追溯原因。",
    ]
    if profile["tier"] in {"epic", "complex"}:
        guidance.append("长线题材需要把旧时代因果拆成多次揭示：误导线索、阶段真相、终局真相不可一次说完。")
    else:
        guidance.append("近关系/现代题材不宜堆砌古老历史，重点让前史服务人物关系、家庭压力或制度惯性。")
    return guidance


def _build_style_adapter(
    *,
    title: str = "",
    premise: str = "",
    genre: str = "",
    world_preset: str = "",
    style_hint: str = "",
) -> dict[str, Any]:
    raw_text = "\n".join(part for part in [style_hint, genre, world_preset, premise, title] if part).strip()
    tags = _detect_style_tags(raw_text)
    primary = tags[0] if tags else "custom_or_unspecified"
    strategy = _style_strategy(primary)
    return {
        "schema_version": 1,
        "mode": "semantic_first_style_late_binding",
        "requested_style": style_hint[:500],
        "detected_style_tags": tags or ["custom_or_unspecified"],
        "primary_style": primary,
        "rendering_strategy": strategy,
        "adaptation_contract": [
            "Evolution 前史是语义蓝图，不是最终正文；规划和写作时必须按小说当前文风重新表达。",
            "保留因果、秘密、代价、伏笔功能，允许彻底改写措辞、节奏、意象、叙述视角和信息密度。",
            "若用户/Bible/章节样本文风与本适配器不一致，以最新显式文风为准。",
            "不要把前史条目机械塞进正文；只能转化为符合文风的场景痕迹、人物选择、传闻、物件或沉默。",
        ],
        "style_axes": {
            "diction": strategy["diction"],
            "sentence_rhythm": strategy["sentence_rhythm"],
            "imagery": strategy["imagery"],
            "information_density": strategy["information_density"],
            "revelation": strategy["revelation"],
        },
    }


def _extract_runtime_style_hint(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("style_hint", "style", "writing_style", "voice", "tone"):
        value = str(payload.get(key) or "").strip()
        if value:
            candidates.append(value)
    bible_context = payload.get("bible_context") if isinstance(payload.get("bible_context"), dict) else {}
    if bible_context:
        for key in ("style_hint", "style", "writing_style", "voice", "tone"):
            value = str(bible_context.get(key) or "").strip()
            if value:
                candidates.append(value)
        for note in bible_context.get("style_notes") or []:
            if isinstance(note, dict):
                content = str(note.get("content") or note.get("description") or "").strip()
                category = str(note.get("category") or "").strip()
                if content:
                    candidates.append(f"{category}: {content}" if category else content)
            else:
                value = str(note or "").strip()
                if value:
                    candidates.append(value)
    return "\n".join(candidates)[:1200]


def _detect_style_tags(text: str) -> list[str]:
    value = str(text or "").lower()
    buckets = [
        ("poetic_lyrical", ["诗", "抒情", "散文", "意象", "唯美", "朦胧", "浪漫", " lyrical", "poetic"]),
        ("plain_realist", ["白描", "现实", "纪实", "克制", "冷静", "平实", "生活流", "realist", "minimal"]),
        ("fast_web_serial", ["爽文", "热血", "节奏快", "强情绪", "打脸", "升级", "网文", "serial"]),
        ("comedic_light", ["轻松", "吐槽", "搞笑", "喜剧", "沙雕", "幽默", "日常向", "comedy"]),
        ("classical_archaic", ["古风", "文言", "典雅", "志怪", "章回", "古典", "classical"]),
        ("hardboiled_noir", ["冷硬", "黑色", "硬汉", "犯罪", "侦探", "noir", "hardboiled"]),
        ("cosmic_ominous", ["克苏鲁", "诡异", "恐怖", "压抑", "阴郁", "不可名状", "ominous", "horror"]),
        ("technical_sf", ["硬科幻", "技术", "赛博", "算法", "工程", "实验", "cyber", "sci-fi", "science fiction"]),
        ("fairytale_fable", ["童话", "寓言", "儿童", "温柔", "治愈", "fairytale", "fable"]),
        ("epic_chronicle", ["史诗", "编年", "群像", "战争史", "王朝", "文明史", "chronicle", "epic"]),
    ]
    tags = [name for name, terms in buckets if any(term in value for term in terms)]
    return tags[:4]


def _style_strategy(primary: str) -> dict[str, str]:
    strategies = {
        "poetic_lyrical": {
            "diction": "用意象、感官和隐喻承载信息，少用制度说明词。",
            "sentence_rhythm": "句式可长短错落，保留回声和余韵。",
            "imagery": "把前史转成物候、颜色、声音、旧物和身体感受。",
            "information_density": "低到中；一次只透露一层情绪化线索。",
            "revelation": "先给象征，再给事实，真相像潮水一样回返。",
        },
        "plain_realist": {
            "diction": "用日常、具体、克制的词，避免宏大抽象名词压过人物生活。",
            "sentence_rhythm": "中短句为主，因果藏在行动和细节里。",
            "imagery": "使用账单、校规、工位、病历、街道等可触摸物。",
            "information_density": "中；每个线索服务一个现实压力。",
            "revelation": "通过人物碰壁、旁人回避、制度流程逐步显影。",
        },
        "fast_web_serial": {
            "diction": "用目标、阻力、赌注、反转来表达前史，保持可读性和推进感。",
            "sentence_rhythm": "短句和强转折更优先。",
            "imagery": "线索要能迅速变成冲突、奖励、惩罚或升级资源。",
            "information_density": "中到高；每幕至少让一条前史因果推动爽点或危机。",
            "revelation": "误导-爆点-更大黑幕，分层抬高期待。",
        },
        "comedic_light": {
            "diction": "用轻巧、反差和吐槽式误会承载严肃因果。",
            "sentence_rhythm": "短促灵活，允许包袱后突然落入真相。",
            "imagery": "把秘密藏在尴尬物件、错位对话和日常事故里。",
            "information_density": "低到中；不要让设定解释压垮喜剧节奏。",
            "revelation": "先当笑点，再在关键处证明笑点是伏笔。",
        },
        "classical_archaic": {
            "diction": "用典雅、含蓄、礼法/名分/旧闻承载因果。",
            "sentence_rhythm": "整饬、留白，少用现代术语。",
            "imagery": "碑、谱牒、旧诏、祠堂、风物和传闻适合承载前史。",
            "information_density": "中；重传承和名分变迁。",
            "revelation": "由传闻、旧物、礼制破绽层层反证。",
        },
        "hardboiled_noir": {
            "diction": "冷、硬、短，重事实、伤痕、交易和背叛。",
            "sentence_rhythm": "短句优先，少解释，多压迫。",
            "imagery": "雨夜、档案袋、烟味、账本、监控盲区等具体痕迹。",
            "information_density": "中高；每条线索都带风险。",
            "revelation": "让真相像旧伤一样被迫撕开。",
        },
        "cosmic_ominous": {
            "diction": "避免直接解释不可名状之物，用异常、缺页、重复梦境和认知污染呈现。",
            "sentence_rhythm": "逐步失稳，允许不完全解释。",
            "imagery": "星象、潮声、畸形仪式、腐蚀文字、无法对齐的时间。",
            "information_density": "低到中；保留未知感。",
            "revelation": "每次解释只揭开更深的不安。",
        },
        "technical_sf": {
            "diction": "用系统、协议、实验、数据缺口和工程限制表达因果。",
            "sentence_rhythm": "清晰准确，避免玄学化。",
            "imagery": "日志、接口、传感器异常、材料疲劳、算法偏差。",
            "information_density": "中高；前史要能支持机制推演。",
            "revelation": "先暴露观测异常，再追溯设计缺陷或历史篡改。",
        },
        "fairytale_fable": {
            "diction": "用简单、温柔、象征性的词承载深层因果。",
            "sentence_rhythm": "明亮、重复、有寓言感。",
            "imagery": "钥匙、门、森林、灯、名字、约定。",
            "information_density": "低；一个象征对应一个秘密。",
            "revelation": "让真相像寓言教训一样自然浮现。",
        },
        "epic_chronicle": {
            "diction": "用编年、誓约、迁徙、王朝和代际代价表达前史。",
            "sentence_rhythm": "稳重，有历史纵深。",
            "imagery": "年表、城邦、血脉、盟约、战场遗址。",
            "information_density": "高；允许多势力、多时代并置。",
            "revelation": "从个人命运回望文明级因果。",
        },
    }
    return strategies.get(
        primary,
        {
            "diction": "跟随用户最新文风提示；无法归类时只保留语义功能，不规定措辞。",
            "sentence_rhythm": "匹配样本文本的句长、停顿和叙述视角。",
            "imagery": "沿用小说自身反复出现的物象，不引入违和符号。",
            "information_density": "弹性；按目标文风决定铺陈或留白。",
            "revelation": "按目标文风选择直给、留白、象征、反转或对话侧写。",
        },
    )


def _hash_text(value: str) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()
