"""Safe text-only templates for programmatic chat replies."""

from __future__ import annotations

from collections.abc import Mapping
from string import Formatter
from typing import Any


DEFAULT_TEMPLATES: dict[str, str] = {
    "menu": (
        "{唤醒词}点歌菜单\n"
        "账号状态：\n"
        "1. 网易云音乐：{登录1}｜会员：{会员1}\n"
        "2. 酷狗音乐：{登录2}｜会员：{会员2}\n"
        "3. QQ音乐：{登录3}｜会员：{会员3}\n"
        "发送“{唤醒词}搜索 歌名”查看歌曲，再发送“{唤醒词}播放 序号”下载并分段发送。"
    ),
    "search_results": (
        "{平台}搜索“{关键词}”找到 {结果数量} 首歌曲：\n"
        "{结果列表}\n"
        "请在 5 分钟内发送“{唤醒词}播放 序号”选择歌曲。"
    ),
    "search_item": "{序号}. 《{歌名}》｜歌手：{歌手}｜专辑：{专辑}｜时长：{时长}｜平台：{平台}",
    "failure": "{动作}失败：{错误原因}",
    "play_selected": "已选择《{歌名}》，正在获取音频并准备分段发送。",
    "play_sending": "《{歌名}》— {歌手}（{平台}），开始按顺序发送音频片段。",
    "play_complete": "《{歌名}》发送完成，共 {已发送段数} 段。",
    "song_log": "[{时间}] 点歌{结果}：《{歌名}》— {歌手}｜平台：{平台}｜触发方式：{触发方式}｜音色：{音色}｜已发送 {已发送段数}/{总段数} 段（{发送时长}）｜来源会话：{来源会话}｜失败原因：{失败原因}",
}

TEMPLATE_CATEGORIES: dict[str, str] = {"song_log": "log"}

REPLY_ENABLED_KEYS = ("play_selected", "play_sending", "play_complete")
REPLY_ENABLED_DEFAULTS = {key: True for key in REPLY_ENABLED_KEYS}


TEMPLATE_VARIABLES: dict[str, dict[str, str]] = {
    "menu": {
        "唤醒词": "当前会话配置的唤醒前缀",
        "登录1": "网易云音乐登录状态",
        "会员1": "网易云音乐会员状态",
        "登录2": "酷狗音乐登录状态",
        "会员2": "酷狗音乐会员状态",
        "登录3": "QQ音乐登录状态",
        "会员3": "QQ音乐会员状态",
    },
    "search_results": {
        "关键词": "本次搜索关键词",
        "平台": "实际搜索的平台",
        "结果数量": "本次搜索到的歌曲数量",
        "结果列表": "按序号排列的歌曲清单",
        "唤醒词": "当前会话配置的唤醒前缀",
    },
    "search_item": {
        "唤醒词": "当前会话配置的唤醒前缀",
        "序号": "歌曲选择序号",
        "歌名": "歌曲名称",
        "歌手": "歌曲演唱者",
        "专辑": "专辑名称",
        "时长": "歌曲时长",
        "平台": "歌曲所在平台",
    },
    "failure": {
        "唤醒词": "当前会话配置的唤醒前缀",
        "动作": "失败的操作，例如搜索或播放",
        "歌名": "当前歌曲名称；没有歌曲时为空",
        "错误原因": "可向用户展示的错误原因",
        "已发送段数": "音频已成功发送的段数；其他错误为 0",
        "总段数": "音频总段数；其他错误为 0",
    },
    "play_selected": {
        "唤醒词": "当前会话配置的唤醒前缀",
        "歌名": "本次选择的歌曲名称",
        "歌手": "本次选择的歌曲演唱者",
        "平台": "实际搜索并选择歌曲的平台",
    },
    "play_sending": {
        "唤醒词": "当前会话配置的唤醒前缀",
        "歌名": "本次选择的歌曲名称",
        "歌手": "本次选择的歌曲演唱者",
        "平台": "歌曲所在平台",
        "总段数": "已准备发送的音频总段数",
        "已发送段数": "当前已成功发送的音频段数",
    },
    "play_complete": {
        "唤醒词": "当前会话配置的唤醒前缀",
        "歌名": "本次选择的歌曲名称",
        "歌手": "本次选择的歌曲演唱者",
        "平台": "歌曲所在平台",
        "总段数": "音频总段数",
        "已发送段数": "成功发送的音频段数",
    },
    "song_log": {
        "时间": "日志发送时间，使用 +08:00 时区",
        "歌名": "歌曲名称",
        "歌手": "歌曲演唱者",
        "平台": "歌曲来源平台",
        "触发方式": "聊天指令、主动LLM或页面手动",
        "音色": "原唱或翻唱音色名称",
        "结果": "成功、部分成功或失败",
        "已发送段数": "成功发送的音频段数",
        "总段数": "计划发送的音频总段数",
        "发送时长": "成功发送音频片段的实际总时长",
        "来源会话": "发起播放任务的会话标识",
        "失败原因": "部分成功或失败原因；成功时为空",
    },
}


_FORMATTER = Formatter()


def _validate_one(name: str, template: Any) -> str:
    if not isinstance(template, str):
        raise ValueError(f"模板“{name}”必须是文字。")
    allowed = TEMPLATE_VARIABLES[name]
    try:
        for _, field_name, format_spec, conversion in _FORMATTER.parse(template):
            if field_name is None:
                continue
            if field_name not in allowed:
                raise ValueError(f"模板“{name}”包含未知变量“{field_name}”。")
            if conversion is not None or format_spec:
                raise ValueError(f"模板“{name}”只支持简单变量替换，不支持格式表达式或属性访问。")
    except ValueError as exc:
        if str(exc).startswith("模板“"):
            raise
        raise ValueError(f"模板“{name}”的花括号格式无效：{exc}") from exc
    return template


def validate_templates(mapping: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate overrides and return them merged with all built-in templates."""
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, Mapping):
        raise ValueError("模板设置必须是键值对象。")
    unknown = [key for key in mapping if key not in DEFAULT_TEMPLATES]
    if unknown:
        raise ValueError(f"存在未知模板键：{', '.join(map(str, unknown))}。")
    result = dict(DEFAULT_TEMPLATES)
    for name, value in mapping.items():
        result[name] = _validate_one(name, value)
    return result


def render_template(name: str, templates: Mapping[str, Any], values: Mapping[str, Any]) -> str:
    """Render a validated template; missing or null values become empty text."""
    if name not in DEFAULT_TEMPLATES:
        raise ValueError(f"未知模板键：{name}。")
    merged = validate_templates(templates)
    safe_values = {
        key: "" if value is None else str(value)
        for key, value in values.items()
    }
    return merged[name].format_map(_MissingValues(safe_values))


class _MissingValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""
