"""标题清洗与噪声判定（采集/整理共用）。

词表优先从外部文件加载，便于在不改代码的情况下扩充：

1. 环境变量 ``NOISE_WORDS_FILE`` 指向的文件；
2. 仓库根目录 ``noise_words.txt``（若存在）；
3. 内置默认词表。

文件格式：每行一个关键词，``#`` 开头为注释，空行忽略。
运行时修改文件后调用 :func:`reload_noise_words` 即可生效（无需重启）。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Iterable, List

logger = logging.getLogger("music_mcp.noise")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 音源侧的基础噪声词（跨音源通用）。
BASE_NOISE_WORDS = (
    "vlog", "路透", "机场", "出发", "抵达", "快闪", "活动", "花絮", "幕后",
    "采访", "访谈", "综艺", "reaction", "直播回放", "演唱会", "巡演",
    "教学", "教程", "讲解", "指弹", "吉他谱", "钢琴课", "琴谱", "光遇",
    "街拍", "生图", "颜值", "混剪", "盘点", "合集",
)

# 采集时额外过滤的噪声词（非本人单曲、话题、活动等）。
EXTRA_NOISE_WORDS = (
    "排名", "排行", "翻唱PK", "PK", "对比", "精选",
    "循环", "歌单", "串烧", "解析", "反应",
    "八卦", "爆料", "争议", "抄袭", "道歉", "回应",
    "清唱", "练唱", "伴奏", "纯音乐", "钢琴", "吉他", "戏腔", "舞蹈",
    "练习室", "直拍", "饭拍",
    "原唱", "谁是", "谁更", "能否", "变声", "模仿", "分集", "带歌词",
    "合唱版", "群星", "跨年", "晚会", "音乐节",
    "首唱", "现场版", "live版", "混音", "remix", "dj",
    # 话题/吐槽/榜单类
    "吐槽", "啥东西", "有多难唱", "难听", "好好听", "半夜爬起来",
    "耳麦", "甜歌", "臻品", "母带", "音质", "完整", "无损", "hi-res",
    # 画质/活动/剧集等非歌名内容
    "1080p", "光流", "60帧", "4k", "8k", "mv", "官方", "好奇夜",
    "盛典", "第二季", "第一季", "主题曲", "插曲", "片尾曲", "片头曲",
    # 游戏/节目/整活/合集
    "生草", "小课堂", "九语", "男版", "女版", "崩坏", "蒙面歌王", "歌手当打",
    "我们的歌", "声入人心", "开场", "串场", "专访", "连线",
    "祝福", "公益", "广告", "宣传片", "预告", "片花", "舞台合集",
    "献礼", "致敬", "纪录片", "春晚", "元宵", "中秋", "国庆", "跨年晚会",
)

_BOOK_TITLE = re.compile(r"《([^》]{1,40})》")
_BLOCK = re.compile(r"[【\[]([^】\]]{2,20})[】\]]")

_noise_words: List[str] = []
_source: str = "builtin"


def _external_file() -> Path:
    import os

    env = os.getenv("NOISE_WORDS_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return PROJECT_ROOT / "noise_words.txt"


def _read_words_file(path: Path) -> List[str]:
    if not path.is_file():
        return []
    words: List[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        words.append(line)
    return words


def _load() -> None:
    global _noise_words, _source
    external = _external_file()
    extra = _read_words_file(external)
    seen = set()
    words: List[str] = []
    # 外部词优先，便于用户覆盖/补充
    for word in [*extra, *EXTRA_NOISE_WORDS, *BASE_NOISE_WORDS]:
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(word)
    _noise_words = words
    _source = str(external) if extra else "builtin"
    if extra:
        logger.info("已加载外部噪声词表 %s（%d 个词）", external, len(extra))


def reload_noise_words() -> List[str]:
    """重新加载词表，返回当前生效的词列表。"""
    _load()
    return list(_noise_words)


def noise_words() -> List[str]:
    if not _noise_words:
        _load()
    return list(_noise_words)


def normalize(text: str) -> str:
    """归一化：全角转半角、小写、去掉标点/空白，仅保留字母数字汉字。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def strip_tags(text: str) -> str:
    import html

    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


def is_noise(title: str) -> bool:
    low = (title or "").lower()
    return any(word.lower() in low for word in noise_words())


def song_name(title: str, artist_hint: str = "") -> str:
    """从标题里提取歌名：优先书名号，其次短方括号块，最后清理后的短标题。"""
    title = strip_tags(title)
    m = _BOOK_TITLE.search(title)
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        if is_noise(name) or (artist_hint and normalize(name) == normalize(artist_hint)):
            return ""
        return name
    # 「【爱错】 王力宏&单依纯 ...」这类：取非噪声的短方括号内容。
    for block in _BLOCK.findall(title):
        candidate = block.strip()
        if len(normalize(candidate)) < 2 or is_noise(candidate):
            continue
        # 「【单依纯】永不失联的爱」这类块只是歌手名，跳过。
        if normalize(candidate) == normalize(artist_hint):
            continue
        return candidate
    text = re.sub(r"[【\[][^】\]]*[】\]]", " ", title)
    text = re.sub(r"[（(][^）)]*[）)]", " ", text)
    # 去掉结尾的连接符 + 歌手名及其前面的合作者列表。
    if artist_hint:
        text = re.sub(
            rf"[\s_\-—|·×&/、]*{re.escape(artist_hint)}\s*$", " ", text
        )
    text = re.sub(r"[-—_|·]+", " ", text)
    text = re.sub(r"\bbgm\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if artist_hint:
        text = re.sub(rf"^{re.escape(artist_hint)}\s*[-—–|·]?\s*", "", text).strip()
    if artist_hint and normalize(text) == normalize(artist_hint):
        return ""
    if re.search(r"(\d{3,4}p|mv\b|光流|帧|官方|高清|高清版)", text, re.I):
        return ""
    return text


def looks_like_song_title(title: str) -> bool:
    """标题是否更像「歌名」而不是视频话题：含书名号《》或总长度较短。"""
    text = (title or "").strip()
    if not text:
        return False
    if "《" in text and "》" in text:
        return True
    core = re.sub(r"[\[\]【】()（）\s\-—_|]+", "", text)
    return len(core) <= 24


def words_matching(title: str, words: Iterable[str] | None = None) -> List[str]:
    """返回标题命中的噪声词，便于整理时解释原因。"""
    low = (title or "").lower()
    pool = list(words) if words is not None else noise_words()
    return [w for w in pool if w.lower() in low]


_load()


_ARTIST_SUFFIXES = (
    "倾情演唱", "神作", "封神之作", "封神", "之作", "倾情", "献唱",
    "演绎", "演唱", "的歌", "的",
)


def clean_artist(artist: str) -> str:
    """规范化歌手字段：去掉「的／演唱／神作」等标题残留。

    多人合作（含 & / 、）保持原样，只清理整体结尾的修饰词。
    """
    text = re.sub(r"\s+", " ", (artist or "")).strip()
    if not text:
        return ""
    changed = True
    while changed:
        changed = False
        for suffix in _ARTIST_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                candidate = text[: -len(suffix)].strip()
                if len(normalize(candidate)) >= 2:
                    text = candidate
                    changed = True
                    break
    return text


# 音质/画质/版本类标记：采集时用作噪声，但整理曲库时不应判定为「非歌曲」。
QUALITY_MARKERS = (
    "hi-res", "hires", "无损", "音质", "母带", "臻品", "4k", "8k", "1080p",
    "60帧", "光流", "mv", "官方", "高清", "纯享", "完整版", "重制",
)

# 整理曲库时只把这些「明确不是歌曲」的话题/活动词当作噪声。
TOPIC_NOISE_WORDS = tuple(
    w for w in EXTRA_NOISE_WORDS if w.lower() not in {m.lower() for m in QUALITY_MARKERS}
)


def is_offtopic(title: str) -> bool:
    """标题是否为明确的话题/活动类内容（不含音质、MV 等版本标记）。"""
    low = (title or "").lower()
    if any(word in low for word in TOPIC_NOISE_WORDS):
        return True
    return any(word in low for word in BASE_NOISE_WORDS)


def offtopic_hits(title: str) -> List[str]:
    low = (title or "").lower()
    pool = [*TOPIC_NOISE_WORDS, *BASE_NOISE_WORDS]
    return [w for w in pool if w.lower() in low]


def safe_song_name(title: str, artist_hint: str = "") -> str:
    """保守地提取歌名：只在有书名号《》时才改写，其余保持原样。

    避免把「【某幻/中国BOY】电子羊」这类方括号里的频道名误当成歌名。
    """
    text = strip_tags(title)
    m = _BOOK_TITLE.search(text)
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        if name and not is_noise(name) and normalize(name) != normalize(artist_hint):
            return name
    return re.sub(r"\s+", " ", text).strip()
