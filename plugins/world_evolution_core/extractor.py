"""Lightweight deterministic fact extractor.

This is intentionally conservative. It only extracts explicit names and places
from committed text so Phase 1 remains fact-driven before LLM extraction is
introduced.
"""
from __future__ import annotations

import re

from .models import ChapterFactSnapshot

_QUOTED_NAME_RE = re.compile(r"[《“‘]([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_·]{1,24})[》”’]")
_TITLE_NAME_RE = re.compile(r"(?:导师|师父|师傅|将军|队长|船长|医生|侦探|公主|王子)([\u4e00-\u9fff]{2,3})(?=站|说|道|问|答|递|看|走|抵达|来到|进入|离开|出现|失踪|醒来|，|。)")
_SURNAME_ACTION_RE = re.compile(
    r"(?<![\u4e00-\u9fff])"
    r"([赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹喻柏水窦章云苏潘葛范彭郎鲁韦昌马苗凤方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于傅齐康伍余顾孟黄穆萧尹姚邵汪祁毛狄米贝明计伏成戴谈宋庞熊纪舒屈项祝董梁杜阮蓝闵季贾路江童颜郭梅盛林钟徐邱骆高夏蔡田胡凌霍虞万卢莫房解应宗丁宣邓郁杭洪左石崔龚程邢裴陆荣翁荀甄曲封储段侯全班秋仲伊宫宁仇甘祖武符刘景龙叶黎白蒲卓蔺池乔闻党翟谭姬申冉雍桑尚温庄晏柴瞿阎慕连习鱼古易廖终居衡步耿满弘匡文寇广东欧沃利蔚越师巩聂晁勾敖融冷辛阚简饶曾沙养鞠丰关查游权益桓岳帅况][\u4e00-\u9fff]{1,2})"
    r"(?=说|问|答|道|低声|沉默|皱眉|抬头|点头|摇头|看|盯|望|走|站|坐|伸手|握|推|拉|递|打开|拿|放|转身|回头|回到|进入|离开|发现|知道|意识到|记得|听见|避开|停下|赶到|拒绝|承认|警告|登记|查到|拦住|拦)"
)
_SURNAME_PARTICLE_ACTION_RE = re.compile(
    r"(?<![\u4e00-\u9fff])"
    r"([赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹喻柏水窦章云苏潘葛范彭郎鲁韦昌马苗凤方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于傅齐康伍余顾孟黄穆萧尹姚邵汪祁毛狄米贝明计伏成戴谈宋庞熊纪舒屈项祝董梁杜阮蓝闵季贾路江童颜郭梅盛林钟徐邱骆高夏蔡田胡凌霍虞万卢莫房解应宗丁宣邓郁杭洪左石崔龚程邢裴陆荣翁荀甄曲封储段侯全班秋仲伊宫宁仇甘祖武符刘景龙叶黎白蒲卓蔺池乔闻党翟谭姬申冉雍桑尚温庄晏柴瞿阎慕连习鱼古易廖终居衡步耿满弘匡文寇广东欧沃利蔚越师巩聂晁勾敖融冷辛阚简饶曾沙养鞠丰关查游权益桓岳帅况][\u4e00-\u9fff]{1,2})"
    r"(?=[从向往在把被与同][^。！？!?]{0,16}(?:来到|进入|离开|赶到|抵达|走|站|看|说|问|答|推|拿|放))"
)
_SURNAME_STATE_ACTION_RE = re.compile(
    r"(?<![\u4e00-\u9fff])"
    r"([赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹喻柏水窦章云苏潘葛范彭郎鲁韦昌马苗凤方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于傅齐康伍余顾孟黄穆萧尹姚邵汪祁毛狄米贝明计伏成戴谈宋庞熊纪舒屈项祝董梁杜阮蓝闵季贾路江童颜郭梅盛林钟徐邱骆高夏蔡田胡凌霍虞万卢莫房解应宗丁宣邓郁杭洪左石崔龚程邢裴陆荣翁荀甄曲封储段侯全班秋仲伊宫宁仇甘祖武符刘景龙叶黎白蒲卓蔺池乔闻党翟谭姬申冉雍桑尚温庄晏柴瞿阎慕连习鱼古易廖终居衡步耿满弘匡文寇广东欧沃利蔚越师巩聂晁勾敖融冷辛阚简饶曾沙养鞠丰关查游权益桓岳帅况][\u4e00-\u9fff]{1,2})"
    r"(?:仍然|仍旧|依旧|已经|正在|突然|立刻|马上|仍|也|已|正|将|却|又|才|都|便|就|会|还|再)?"
    r"(?=说|问|答|道|低声|沉默|皱眉|抬头|点头|摇头|看|盯|望|走|站|坐|伸手|握|推|拉|递|打开|拿|放|转身|回头|回到|进入|离开|发现|知道|意识到|记得|听见|避开|停下|赶到|拒绝|承认|警告|登记|查到|抵达|拦住|拦)"
)
_NICKNAME_ACTION_RE = re.compile(
    r"(?<![\u4e00-\u9fff])"
    r"(阿[\u4e00-\u9fff]{1,2})"
    r"(?=说|问|答|道|低声|沉默|皱眉|抬头|点头|摇头|看|盯|望|走|站|坐|伸手|握|推|拉|递|打开|拿|放|转身|回头|回到|进入|离开|发现|知道|意识到|记得|听见|避开|停下|赶到|拒绝|承认|警告|登记|查到|抵达|拦住|拦)"
)
_LOCATION_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9_·]{1,10}(?:城|镇|村|山|谷|宫|殿|塔|港|湖|河|海|岛|森林|学院|基地|星|站|街|巷|门|府))")
_LOCATION_PREFIX_RE = re.compile(r"^(?:抵达|来到|进入|离开|前往|返回|经过|穿过|发现|整座|半张|一座|那座|这座)+")
_EVENT_SPLIT_RE = re.compile(r"[。！？!?\n]+")

_STOP_NAMES = {
    "主角",
    "少年",
    "少女",
    "男子",
    "女子",
    "老人",
    "学院",
    "导师",
    "监察",
    "圣像",
    "黑潮",
    "雾港",
    "文字",
    "金属牌",
    "方向",
    "查询记录",
    "记录",
    "编号",
    "债务",
    "契约",
    "防火门",
    "黑色书籍",
    "书籍",
    "访客卡",
    "臂章",
    "钥匙",
    "黑匣子",
}
_BAD_NAME_FRAGMENTS = ("的", "了", "在", "并", "和", "得")
_BAD_NAME_PREFIXES = (
    "很",
    "也",
    "又",
    "都",
    "还",
    "再",
    "才",
    "更",
    "最",
    "真",
    "真正",
    "明知",
    "如果",
    "但是",
    "只是",
    "没有",
)
_BAD_NAME_KEYWORDS = (
    "教程",
    "基础教程",
    "说明",
    "系统",
    "标记",
    "文字",
    "章节",
    "第",
)
_BAD_NAME_SUFFIXES = (
    "从",
    "向",
    "往",
    "把",
    "被",
    "与",
    "同",
    "到",
    "在",
    "站",
    "坐",
    "走",
    "看",
    "问",
    "答",
    "说",
    "道",
    "留",
    "仍",
    "也",
    "已",
    "正",
    "将",
    "却",
    "又",
    "才",
    "都",
    "便",
    "就",
    "会",
    "还",
    "再",
    "没",
    "知",
    "懂",
    "想",
    "能",
    "要",
    "压",
    "拦",
    "点",
    "停",
    "让",
    "给",
    "把",
    "对",
    "错",
)
_LOCATION_VERBS = ("抵达", "来到", "进入", "离开", "前往", "返回", "经过", "穿过", "发现")
_LOCATION_NOUNS = (
    "城",
    "镇",
    "村",
    "山",
    "谷",
    "宫",
    "殿",
    "塔",
    "港",
    "湖",
    "河",
    "海",
    "岛",
    "森林",
    "学院",
    "基地",
    "星",
    "站",
    "街",
    "巷",
    "门",
    "府",
    "馆",
    "库",
    "楼",
    "层",
    "室",
    "厅",
    "房",
    "井",
    "渠",
    "平台",
    "工坊",
    "机房",
)
_BAD_LOCATION_PREFIXES = ("但", "而", "却", "然后", "老板", "个", "道", "那道", "这道", "半张", "大多", "根据")
_BAD_LOCATION_ACTIONS = ("咬牙", "站", "说", "问", "看", "推", "拉", "拿", "放", "打开", "闭上", "沉默")


def extract_chapter_facts(novel_id: str, chapter_number: int, content_hash: str, content: str, at: str) -> ChapterFactSnapshot:
    summary = _summary(content)
    characters = _dedupe(_extract_characters(content))[:12]
    locations = _dedupe(
        location
        for location in (_normalize_location(match.group(1)) for match in _LOCATION_RE.finditer(content))
        if _valid_location(location)
    )[:12]
    events = _dedupe(_extract_events(content))[:8]
    return ChapterFactSnapshot(
        novel_id=novel_id,
        chapter_number=chapter_number,
        content_hash=content_hash,
        summary=summary,
        characters=characters,
        locations=locations,
        world_events=events,
        at=at,
    )


def _summary(content: str, limit: int = 500) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    return compact[:limit]


def _extract_characters(content: str):
    for match in _QUOTED_NAME_RE.finditer(content):
        name = match.group(1).strip()
        if _valid_name(name):
            yield name
    for match in _TITLE_NAME_RE.finditer(content):
        name = match.group(1).strip()
        if _valid_name(name):
            yield name
    for match in _SURNAME_PARTICLE_ACTION_RE.finditer(content):
        name = match.group(1).strip()
        if _valid_name(name, normalize=False):
            yield name
    for match in _NICKNAME_ACTION_RE.finditer(content):
        name = match.group(1).strip()
        if _valid_name(name):
            yield name
    for match in _SURNAME_ACTION_RE.finditer(content):
        name = _normalize_name(match.group(1))
        if _valid_name(name):
            yield name
    for match in _SURNAME_STATE_ACTION_RE.finditer(content):
        name = _normalize_name(match.group(1))
        if _valid_name(name):
            yield name


def _extract_events(content: str):
    for sentence in _EVENT_SPLIT_RE.split(content):
        sentence = sentence.strip()
        if len(sentence) < 8:
            continue
        if any(token in sentence for token in ("抵达", "来到", "进入", "离开", "发现", "失踪", "死亡", "袭击", "结盟", "背叛", "开战", "爆发")):
            yield sentence[:160]


def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _normalize_location(value: str) -> str:
    location = _LOCATION_PREFIX_RE.sub("", str(value or "").strip())
    for verb in _LOCATION_VERBS:
        if verb in location:
            location = location.split(verb)[-1]
    if len(location) > 10:
        for suffix in ("森林", "学院", "基地"):
            if location.endswith(suffix):
                return location[-(len(suffix) + 4):]
    return location


def _valid_location(value: str) -> bool:
    location = str(value or "").strip()
    if len(location) < 2:
        return False
    if location.startswith(_BAD_LOCATION_PREFIXES):
        return False
    if any(action in location[:-1] for action in _BAD_LOCATION_ACTIONS):
        return False
    if location in {"道防火门", "个信息站", "老板专门", "但他咬牙站"}:
        return False
    return location.endswith(_LOCATION_NOUNS)


def _normalize_name(value: str) -> str:
    name = str(value or "").strip()
    while len(name) > 2 and name.endswith(_BAD_NAME_SUFFIXES):
        name = name[:-1]
    return name


def _valid_name(value: str, *, normalize: bool = True) -> bool:
    name = _normalize_name(value) if normalize else str(value or "").strip()
    if not name or name in _STOP_NAMES:
        return False
    if len(name) < 2:
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]+", name) and len(name) > 5:
        return False
    if name.startswith(_BAD_NAME_PREFIXES):
        return False
    if any(keyword in name for keyword in _BAD_NAME_KEYWORDS):
        return False
    if any(keyword in name for keyword in ("金属", "记录", "方向", "编号", "钥匙", "防火门", "书籍")):
        return False
    if name.endswith(_BAD_NAME_SUFFIXES):
        return False
    if re.search(r"[一二三四五六七八九十百千万0-9]", name):
        return False
    return not any(fragment in name for fragment in _BAD_NAME_FRAGMENTS)
