# -*- coding: utf-8 -*-
"""
画像引擎 —— 《信息流知道你》Demo 核心逻辑

严格实现《开发方案》：
  §8.2  兴趣域评分：原始分累积 + 软饱和归一化（无时间衰减）
  §8.3  特质评分：Beta 后验均值，负权重记入对立极
  §8.4  行为权重 + scroll_depth 校验 + 三条约束（不叠加/停留上限/返回一次）
  §8.5  位次偏差修正
  §8.6  置信度与展示强度分离
  §8.8  增量更新（幂等去重）
  §9    收敛策略：首屏正交探针 / 分歧度采样 / 成对投放
  §10   Evidence（含跨域证据）
  §11   每屏组成 2/3 兴趣 + 1/3 探索，去重与新鲜度
  §3.3  行为观察句式，不做人格定论
  §14.3 广告匹配（含 coverage 修正）

当前手机端结束流程由行为收敛或屏数上限触发，不使用额外的消歧提问；
消歧接口仅供离线调用，线上页面不会调用或展示。

全部白盒规则，零外部依赖，断网可跑。会话/画像只在内存；如调用方显式
提供 metrics_path，仅固定白名单中的汇总计数会原子写入。
"""

import json
import math
import os
import random
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------- 参数（§8.9 校准位）

KAPPA = 3.5                 # 软饱和系数，唯一需要现场调的参数（由 simulate.py 扫出）
DOMAIN_W = 1.0              # 兴趣域标签权重
POS_BIAS = 0.15             # 位次修正斜率
RETURN_BONUS = 0.4          # 返回查看追加权重
DWELL_CAP_MS = 15000        # 停留上限 15s
CONF_N = 3                  # 满置信所需证据数
NEUTRAL_LO, NEUTRAL_HI = 0.35, 0.65    # 特质中性区，区内不展示
CONVERGE_SCORE = 0.70
SCREEN_SIZE = 6
MAX_PER_SUBTAG = 2          # §11.3 同一子标签单屏最多 2 条
MAX_PER_DOMAIN = 3          # §11.3 同一域单屏最多 3 条
SESSION_TTL = 2 * 3600      # §6.3 体验数据 2 小时后自动删除

# 长期留存只允许这些“没有会话维度”的累计数字。任何 session id、随机代号、
# 内容 id、点击序号、画像或时间线都不应进入落盘文件；新增指标必须先经过
# 这里的白名单，避免一次无意的字段扩展破坏隐私承诺。
AGGREGATE_METRIC_KEYS = (
    "participants_total",       # 创建过的全部体验人数（含不上大屏者）
    "feedback_accurate",         # 最终选择“准”的会话数
    "feedback_inaccurate",       # 最终选择“不准”的会话数
)
AGGREGATE_METRICS_VERSION = 2


class AggregateMetricsStore:
    """只保存聚合计数的原子 JSON 存储。

    会话和画像仍只存在内存；这个类的输入和输出都经过固定白名单过滤，
    因而即使调用方传入了额外字段，也不会把个体数据写入文件。``path=None``
    用于离线仿真或单元调用，表示完全关闭落盘。
    """

    def __init__(self, path=None):
        if path is None or str(path).strip() == "":
            self.path = None
        else:
            try:
                self.path = Path(path).expanduser()
            except (TypeError, ValueError, RuntimeError, OSError):
                # 非法配置不应让体验服务在导入阶段崩溃；退回内存模式，
                # 同时由 aggregate_stats 的 persistence_enabled 告知后台。
                self.path = None
        self.lock = threading.RLock()
        self.last_write_ok = True
        self._metrics = self._empty_metrics()
        self._load()

    @staticmethod
    def _empty_metrics():
        return {key: 0 for key in AGGREGATE_METRIC_KEYS}

    @staticmethod
    def _count(value):
        # bool 是 int 的子类，但不应被当作合法计数；浮点数也拒绝，避免
        # 损坏文件悄悄改变累计口径。
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str) and value.strip().isdigit():
            try:
                return max(0, int(value.strip()))
            except (TypeError, ValueError, OverflowError):
                return 0
        return 0

    @classmethod
    def _sanitize(cls, raw):
        out = cls._empty_metrics()
        if not isinstance(raw, dict):
            return out
        for key in AGGREGATE_METRIC_KEYS:
            out[key] = cls._count(raw.get(key, 0))
        return out

    def _load(self):
        if self.path is None:
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            doc = json.loads(raw)
            # 只接受新格式的 metrics 对象；忽略其它顶层字段和未知键，
            # 这样旧文件即使意外包含会话字段，也不会被带入内存或再次落盘。
            if not isinstance(doc, dict) or not isinstance(doc.get("metrics"), dict):
                raise ValueError("aggregate metrics must contain a metrics object")
            source = doc["metrics"]
            self._metrics = self._sanitize(source)
            needs_migration = (
                doc.get("version") != AGGREGATE_METRICS_VERSION or
                set(source) != set(AGGREGATE_METRIC_KEYS) or
                set(doc) != {"version", "metrics"}
            )
            # 旧版本在加载时立即按当前白名单重写；这样即使现场暂时没有
            # 新会话，已经取消的行为指标也不会继续留在磁盘文件里。
            self.last_write_ok = self._persist_unlocked() if needs_migration else True
        except FileNotFoundError:
            # 首次启动尚未有统计文件是正常状态；第一次变更时会创建它。
            self._metrics = self._empty_metrics()
        except (OSError, UnicodeError, ValueError, TypeError):
            # 半写入或人工编辑损坏时从空桶继续，不阻断体验；后台应显示
            # 保存状态异常，提醒维护者检查文件，而不是假报健康。
            self._metrics = self._empty_metrics()
            self.last_write_ok = False

    def snapshot(self):
        with self.lock:
            return dict(self._metrics)

    def replace(self, metrics):
        """替换全部白名单计数，并尽力原子写入；失败时保留内存值。"""
        clean = self._sanitize(metrics)
        with self.lock:
            self._metrics = clean
            self.last_write_ok = self._persist_unlocked()
            return self.last_write_ok

    def _persist_unlocked(self):
        if self.path is None:
            return True
        tmp_path = None
        fd = None
        try:
            parent = self.path.parent
            parent.mkdir(parents=True, exist_ok=True)
            # 临时文件与目标文件放在同一目录，os.replace 才能保持同卷原子性。
            fd, name = tempfile.mkstemp(prefix=".%s." % self.path.name,
                                        suffix=".tmp", dir=str(parent))
            tmp_path = Path(name)
            payload = {
                "version": AGGREGATE_METRICS_VERSION,
                "metrics": {key: int(self._metrics.get(key, 0))
                            for key in AGGREGATE_METRIC_KEYS},
            }
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = None  # fdopen 接管描述符，异常清理由上下文管理器负责
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    # 某些文件系统/沙箱不提供 fsync；同卷 replace 仍能提供
                    # 比直接覆盖更好的抗半写入能力。
                    pass
            os.replace(str(tmp_path), str(self.path))
            tmp_path = None
            return True
        except (OSError, TypeError, ValueError):
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def _finite_number(value, default=0.0):
    """把客户端传入的数字安全地归一为有限浮点数。

    手机端事件来自网络，旧 WebView 或手工请求可能把数字编码成字符串，
    也可能传入 NaN/Infinity。评分函数不应因为这类输入抛异常，更不能让
    一个异常请求中断后续的推荐批次。
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _safe_event_timestamp(value, default=None):
    """把事件时间戳固定为非负整数，拒绝任意文本/对象注入输出。"""
    if default is None:
        default = time.time() * 1000
    number = _finite_number(value, default)
    try:
        return int(max(0.0, number))
    except (TypeError, ValueError, OverflowError):
        return int(max(0.0, _finite_number(default, 0.0)))


def _safe_feed_name(value, default="home"):
    """只接受前端约定的信息流来源名，避免把任意文本带进画像输出。"""
    if isinstance(value, str):
        name = value.strip().lower()
        if name in ("home", "discover"):
            return name
    return default if default in ("home", "discover") else "home"


def _safe_bool(value, default=False):
    """解析可见性等布尔选项，避免字符串 ``\"false\"`` 被当成真值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("true", "1", "yes", "on"):
            return True
        if token in ("false", "0", "no", "off"):
            return False
    return bool(default)


def _context_seq(value):
    """Return a non-negative integer context sequence, or ``None``.

    The browser sends the click sequence in a query string for the early
    ``/api/content?open=1`` hint and in the event body for the normal click
    event.  Treat malformed values as absent rather than allowing a bad
    request to break recommendation generation.  Keeping this separate from
    ``_finite_number`` also prevents fractional/NaN values from becoming a
    fake ordering token.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\+?\d+", text):
            try:
                return int(text)
            except (TypeError, ValueError, OverflowError):
                return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or number != math.floor(number):
        return None
    return int(number)

# 首屏探针轮换：两组都覆盖 8 个学生兴趣域，但每个域交替取一条探针。
# 用 content_id 而不是「域 + random.choice」表达轮换，避免连续场次在同一时段
# 恰好抽到相同的 8 张卡（改进建议 E.16 / 内容库设计 §5.3）。
PROBE_GROUPS = {
    "default": ["P01", "P03", "P05", "P07", "P09", "P11", "P13", "P15"],
    "campus":  ["P02", "P04", "P06", "P08", "P10", "P12", "P14", "P16"],
}
MAX_POSITION_INDEX = max([SCREEN_SIZE] + [len(v) for v in PROBE_GROUPS.values()]) - 1

# ---------------------------------------------------------------- 标签体系（§7.2）

TRAIT_AXIS = {
    "price_sensitive":  ("price",     "pro"),
    "premium_oriented": ("price",     "con"),
    "deep_reader":      ("depth",     "pro"),
    "quick_skimmer":    ("depth",     "con"),
    "early_adopter":    ("novelty",   "pro"),
    "conservative":     ("novelty",   "con"),
    "professional":     ("expertise", "pro"),
    "casual":           ("expertise", "con"),
    "decisive":         ("decision",  "pro"),
    "comparer":         ("decision",  "con"),
}
OPPOSITE = {"pro": "con", "con": "pro"}
AXES = ["price", "depth", "novelty", "expertise", "decision"]
AXIS_CN = {"price": "价格取向", "depth": "阅读深度", "novelty": "尝鲜度",
           "expertise": "专业度", "decision": "决策方式"}
POLE_NAME = {  # axis -> (正极 trait, 负极 trait)
    "price": ("price_sensitive", "premium_oriented"),
    "depth": ("deep_reader", "quick_skimmer"),
    "novelty": ("early_adopter", "conservative"),
    "expertise": ("professional", "casual"),
    "decision": ("decisive", "comparer"),
}
TRAIT_CN = {
    "price_sensitive": "价格敏感", "premium_oriented": "品质优先",
    "deep_reader": "深度阅读", "quick_skimmer": "快速浏览",
    "early_adopter": "乐于尝鲜", "conservative": "偏好稳妥",
    "professional": "专业向", "casual": "大众向",
    "decisive": "决策果断", "comparer": "反复对比",
}
DOMAIN_CN = {
    "tech_digital": "科技数码", "game_acg": "游戏动漫", "life_shopping": "生活消费",
    "sport_outdoor": "运动户外", "av_ent": "影视娱乐", "culture_art": "文化艺术",
    "study_exam": "学习考试", "social_hot": "社交热点",
}
SUBTAG_CN = {
    "ai": "人工智能", "programming": "编程", "hardware": "硬件", "gadget": "数码好物",
    "mobile": "手机", "devops": "运维部署", "robot": "机器人",
    "pc_game": "PC 游戏", "mobile_game": "手游", "anime": "动漫", "esports": "电竞",
    "indie": "独立游戏", "acg_goods": "周边谷子",
    "food": "美食", "home": "家居", "kitchen": "厨房", "pet": "宠物",
    "appliance": "家电", "daily": "日常好物",
    "reading": "阅读", "history": "历史", "language": "语言", "museum": "文博",
    "science": "科普", "scifi": "科幻",
    "fitness": "健身", "ball_game": "球类", "camping": "露营", "running": "跑步",
    "cycling": "骑行", "hiking": "徒步", "gear": "装备",
    "movie": "电影", "music": "音乐", "variety": "综艺", "show": "演出",
    "audio": "音响", "streaming": "流媒体",
    "course": "课程", "exam": "复习考试", "note": "笔记错题",
    "lab": "实验报告", "group_work": "小组作业", "campus_tool": "校园工具",
    "campus_life": "校园生活", "club": "社团活动", "trend": "热点话题",
    "volunteer": "志愿活动", "news_literacy": "信息核验", "campus_notice": "校园公共事务",
}

# 运行时只拦截需要人工复核的组合词；不要使用单字「定位」「党」「附近」等
# 容易误伤正常内容的词。seed 加载时命中会直接拒绝启动，避免审核后又被
# 误投到线上（改进建议 C.11）。
RUNTIME_BLOCKED_PHRASES = ("实时定位", "GPS 定位", "你附近")

# 标签体系的单一事实来源。保留上面的内置值是为了让离线仿真在被单独
# 拷贝、没有事件库目录时仍能启动；正式 demo 启动时会优先读取
# `事件库/taxonomy.json`，并校验域、轴、两极之间的引用关系。
TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "事件库" / "taxonomy.json"


def _load_taxonomy(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read taxonomy.json: %s" % exc) from exc
    if not isinstance(data, dict):
        raise ValueError("taxonomy.json must contain an object")

    domains = data.get("domains")
    order = data.get("domain_order")
    if not isinstance(domains, dict) or not domains:
        raise ValueError("taxonomy domains are missing")
    if not isinstance(order, list) or set(order) != set(domains):
        raise ValueError("taxonomy domain_order does not match domains")
    domain_cn = {}
    for key in order:
        meta = domains.get(key)
        if not isinstance(meta, dict) or not isinstance(meta.get("cn"), str) or not meta["cn"].strip():
            raise ValueError("taxonomy domain %s has no cn label" % key)
        domain_cn[key] = meta["cn"].strip()

    subtags = data.get("sub_tags")
    if not isinstance(subtags, dict) or not subtags:
        raise ValueError("taxonomy sub_tags are missing")
    subtag_cn = {}
    for key, value in subtags.items():
        if isinstance(value, dict):
            value = value.get("cn")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("taxonomy sub_tag %s has no cn label" % key)
        subtag_cn[key] = value.strip()

    axes_data = data.get("axes")
    axes = data.get("axis_order")
    if not isinstance(axes_data, dict) or not isinstance(axes, list) or not axes:
        raise ValueError("taxonomy axes are missing")
    if set(axes) != set(axes_data):
        raise ValueError("taxonomy axis_order does not match axes")
    axis_cn, pole_name = {}, {}
    for axis in axes:
        meta = axes_data.get(axis)
        if not isinstance(meta, dict) or not isinstance(meta.get("cn"), str):
            raise ValueError("taxonomy axis %s is invalid" % axis)
        pro, con = meta.get("pro"), meta.get("con")
        if not isinstance(pro, str) or not isinstance(con, str) or pro == con:
            raise ValueError("taxonomy axis %s must define two poles" % axis)
        axis_cn[axis] = meta["cn"].strip()
        pole_name[axis] = (pro, con)

    traits_data = data.get("traits")
    if not isinstance(traits_data, dict) or not traits_data:
        raise ValueError("taxonomy traits are missing")
    trait_cn, trait_axis = {}, {}
    for trait, meta in traits_data.items():
        if not isinstance(meta, dict):
            raise ValueError("taxonomy trait %s is invalid" % trait)
        axis, pole, label = meta.get("axis"), meta.get("pole"), meta.get("cn")
        if axis not in axis_cn or pole not in ("pro", "con") or not isinstance(label, str) or not label.strip():
            raise ValueError("taxonomy trait %s has invalid axis/pole" % trait)
        trait_cn[trait] = label.strip()
        trait_axis[trait] = (axis, pole)
    for axis, (pro, con) in pole_name.items():
        if trait_axis.get(pro) != (axis, "pro") or trait_axis.get(con) != (axis, "con"):
            raise ValueError("taxonomy axis %s pole references are inconsistent" % axis)

    groups = data.get("probe_groups") or {}
    if not isinstance(groups, dict):
        raise ValueError("taxonomy probe_groups must be an object")
    probe_groups = {}
    for name, ids in groups.items():
        if not isinstance(ids, list) or not all(isinstance(cid, str) and cid for cid in ids):
            raise ValueError("taxonomy probe group %s is invalid" % name)
        probe_groups[name] = list(ids)

    blocked = data.get("runtime_blocked_phrases") or []
    if not isinstance(blocked, list) or not all(isinstance(x, str) and x for x in blocked):
        raise ValueError("taxonomy runtime_blocked_phrases is invalid")
    return {
        "domain_cn": domain_cn, "subtag_cn": subtag_cn,
        "axes": list(axes), "axis_cn": axis_cn, "pole_name": pole_name,
        "trait_cn": trait_cn, "trait_axis": trait_axis,
        "probe_groups": probe_groups, "blocked": tuple(blocked),
    }


_TAXONOMY = _load_taxonomy(TAXONOMY_PATH)
if _TAXONOMY:
    DOMAIN_CN = _TAXONOMY["domain_cn"]
    SUBTAG_CN = _TAXONOMY["subtag_cn"]
    AXES = _TAXONOMY["axes"]
    AXIS_CN = _TAXONOMY["axis_cn"]
    POLE_NAME = _TAXONOMY["pole_name"]
    TRAIT_CN = _TAXONOMY["trait_cn"]
    TRAIT_AXIS = _TAXONOMY["trait_axis"]
    if _TAXONOMY["probe_groups"]:
        PROBE_GROUPS = _TAXONOMY["probe_groups"]
    RUNTIME_BLOCKED_PHRASES = _TAXONOMY["blocked"]
    MAX_POSITION_INDEX = max([SCREEN_SIZE] + [len(v) for v in PROBE_GROUPS.values()]) - 1

def cn(key):
    return DOMAIN_CN.get(key) or SUBTAG_CN.get(key) or TRAIT_CN.get(key) or key

# ---------------------------------------------------------------- 消歧卡（兼容离线仿真；手机端结束流程不再调用）
DISAMBIG_CARDS = [
    ("Q1",  "price",     "同一件东西，你更在意",   "性价比高，外观普通", "贵一些，做工更好"),
    ("Q2",  "price",     "你更可能点开",           "《五百元档怎么选》", "《顶配到底强在哪》"),
    ("Q3",  "depth",     "你更想看",               "一篇三千字的完整拆解", "一张图，三十秒看完"),
    ("Q4",  "depth",     "遇到感兴趣的话题，你会", "找一篇长文认真读完", "先看摘要，够用就行"),
    ("Q5",  "novelty",   "你更想了解",             "刚发布的新东西", "用了很多年、口碑稳的"),
    ("Q6",  "novelty",   "新版本出来时，你通常",   "第一时间就更新", "等等看再说"),
    ("Q7",  "expertise", "你更喜欢哪种讲法",       "术语准确，面向内行", "讲得通俗，谁都能懂"),
    ("Q8",  "expertise", "看到一堆参数表，你会",   "逐项看完", "直接跳到结论"),
    ("Q9",  "decision",  "你更想要",               "直接告诉我选哪个", "给我几个选项自己比"),
    ("Q10", "decision",  "做决定前，你一般",       "想好了就定", "反复看好几家"),
]
DISAMBIG_W_PICKED = 1.5     # §8.4
DISAMBIG_W_UNPICKED = -0.5

# ---------------------------------------------------------------- 行为标签（开发方案 §3.3）

PERSONA_FALLBACK_DOMAIN = {d: f"{c}浏览偏好" for d, c in DOMAIN_CN.items()}

# ---------------------------------------------------------------- 代号（匿名、多格式；不把观众固定成动物）

# 代号只用于现场展示和本次会话内的称呼，与画像维度完全无关。
# 以前的「颜色 + 小动物」组合过于单一，也容易让匿名标识显得像人格标签；
# 现在从抽象词、短码和颜色词的多种格式中随机取一种，并带短校验片段避免撞名。
COLORS = ["蓝色", "橙色", "青色", "紫色", "赤色", "银色", "墨绿", "杏色",
          "靛蓝", "琥珀", "薄荷", "绛红"]
CODE_WORDS = ["坐标", "回声", "脉冲", "折线", "光谱", "矩阵", "轨迹", "采样",
              "序列", "信标", "切面", "回路", "变量", "索引", "节点", "视窗",
              "帧格", "量级", "余弦", "频段", "接口", "刻度", "映射", "分岔"]
CODE_PREFIXES = ["AX", "Q7", "N3", "R2", "K8", "M4", "T6", "Z1"]


def make_codename():
    """生成短、可读、与身份无关的匿名代号。

    四种格式混用，让连续参与者不会总看到同一类模板化名字；
    UUID 片段只作为防碰撞尾码，不携带设备或用户信息。
    """
    token = uuid.uuid4().hex.upper()
    rng = random.Random(token)
    style = int(token[:2], 16) % 4
    word = rng.choice(CODE_WORDS)
    tail = token[2:6]
    if style == 0:
        return f"{rng.choice(COLORS)}·{word}-{tail}"
    if style == 1:
        return f"{word}-{tail}"
    if style == 2:
        return f"{rng.choice(CODE_PREFIXES)}-{tail}"
    return f"{rng.choice(COLORS)}/{rng.choice(CODE_WORDS)}·{tail}"


# ================================================================ 内容库

class Library:
    """内容 / 广告的只读内存库（学生版约 103 条兴趣内容，规模可全量内存打分）"""

    def __init__(self, seed_dir):
        seed_dir = Path(seed_dir)
        with open(seed_dir / "contents.seed.json", encoding="utf-8") as f:
            cdata = json.load(f)
        with open(seed_dir / "ads.seed.json", encoding="utf-8") as f:
            adata = json.load(f)
        # 消歧卡从内容库种子读取；没有有效种子时使用内置兜底。
        self.disambig_cards = self._load_disambig_cards(seed_dir / "cards.seed.json")

        self.contents = {}
        for c in cdata["contents"]:
            text = "%s%s%s" % (c.get("title", ""), c.get("summary", ""), c.get("body", ""))
            blocked = next((p for p in RUNTIME_BLOCKED_PHRASES if p in text), None)
            if blocked:
                raise ValueError("content %s contains blocked phrase %s" %
                                 (c.get("content_id", "?"), blocked))
            c.setdefault("traits", {})
            c["traits"] = c["traits"] or {}
            c["sub_tags"] = c.get("sub_tags") or {}
            self.contents[c["content_id"]] = c

        self.ads = adata["ads"]
        self.probes = [c for c in self.contents.values() if c.get("is_probe")]
        self.feed_pool = [c for c in self.contents.values() if not c.get("is_probe")]
        self.by_domain = defaultdict(list)
        for c in self.feed_pool:
            self.by_domain[c["domain"]].append(c)
        self.pairs = self._build_pairs()

    @staticmethod
    def _load_disambig_cards(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            out = []
            for card in data.get("cards", []):
                if not isinstance(card, dict):
                    continue
                options = card.get("options") or []
                if (not isinstance(card.get("card_id"), str) or
                        card.get("axis") not in AXES or len(options) != 2):
                    continue
                if any(not isinstance(o, dict) for o in options):
                    continue
                poles = {o.get("pole") for o in options}
                if poles != {"pro", "con"} or any(not o.get("text") for o in options):
                    continue
                by_pole = {o["pole"]: o["text"] for o in options}
                out.append((card["card_id"], card["axis"], card.get("prompt", ""),
                            by_pole["pro"], by_pole["con"]))
            if out:
                return out
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        return list(DISAMBIG_CARDS)

    def _build_pairs(self):
        """§9.3 成对投放：同域 + 同轴对立，加载期一次自动枚举。

        设计文档曾维护一张手工表，但学生版扩充后容易与 seed 脱节；现在以
        seed 标签为单一事实来源，保留同样的三元组接口，并额外缓存纯净度供
        运行时排序。纯净度高表示两条内容长度一致、没有额外对立轴。
        """
        pairs = defaultdict(list)          # domain -> [(axis, c_pro, c_con)]
        self.pair_purity = {}
        for domain, items in self.by_domain.items():
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    for ta, wa in a["traits"].items():
                        axis, pole_a = TRAIT_AXIS[ta]
                        for tb, wb in b["traits"].items():
                            axis_b, pole_b = TRAIT_AXIS[tb]
                            if axis_b != axis or pole_b == pole_a:
                                continue
                            pro, con = (a, b) if pole_a == "pro" else (b, a)
                            item = (axis, pro["content_id"], con["content_id"])
                            pairs[domain].append(item)
                            self.pair_purity[(domain, axis, pro["content_id"], con["content_id"])] = \
                                self._pair_purity(pro, con, axis)
        return pairs

    @staticmethod
    def _pair_purity(pro, con, axis):
        """返回 0~1 的配对纯净度，供探索位轻量排序。"""
        value = 1.0
        if pro.get("body_len") != con.get("body_len"):
            value -= 0.25
        for c in (pro, con):
            for trait in c.get("traits", {}):
                if trait in TRAIT_AXIS and TRAIT_AXIS[trait][0] != axis:
                    value -= 0.12
        return max(0.0, round(value, 3))

    def card(self, c):
        """投放给手机端的卡片视图（不含正文，点击后再取）"""
        return {
            "content_id": c["content_id"], "title": c["title"],
            "summary": c["summary"], "domain": c["domain"],
            "domain_cn": c.get("domain_cn", DOMAIN_CN.get(c["domain"], "")),
            "cover_theme": c.get("cover_theme", "tech"),
            "body_len": c.get("body_len", "S"),
        }


# ================================================================ 会话与画像

def position_factor(position):
    """§8.5 位次偏差修正：越靠后仍被点开，代表更强主动兴趣。"""
    pos = max(0, min(int(_finite_number(position, 0)), MAX_POSITION_INDEX))
    return 1.0 + POS_BIAS * pos


def dwell_weight(dwell_ms, scroll_depth, body_len):
    """§8.4 停留权重 + scroll_depth 有效性校验（短文不校验）"""
    dwell_ms = max(0.0, min(_finite_number(dwell_ms, 0), DWELL_CAP_MS))
    scroll_depth = max(0.0, min(_finite_number(scroll_depth, 0), 1.0))
    if dwell_ms >= 15000:
        if body_len == "S":
            return 1.0
        return 1.0 if scroll_depth >= 0.6 else 0.7
    if dwell_ms >= 5000:
        return 0.7
    return 0.5 if dwell_ms > 0 else 0.0


class Session:
    def __init__(self, lib, probe_group="default"):
        self.lib = lib
        # ThreadingHTTPServer 会同时处理事件上报、推荐请求和画像轮询。
        # 这些操作共享最近点击、seen 与评分状态；RLock 允许内部评分函数
        # 继续互相调用，同时保证下一批不会读到半个事件的中间状态。
        self.lock = threading.RLock()
        # 会话编号只用于当前浏览器与服务端通信；使用完整随机 UUID，避免
        # 旁观者通过枚举短编号读取别人的结果。
        self.sid = "sess_" + uuid.uuid4().hex
        self.codename = make_codename()
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.probe_group = probe_group

        self.seen_seq = set()
        self.applied_w = {}                       # content_id -> 已记入的权重
        self.returned = set()
        self.seen = set()                         # 已投放内容
        # 最近一次真正打开的内容，作为下一批推荐的短期上下文。
        # 这与累计画像分开保存：画像负责排序，最近点击只用于下一批内容的衔接
        # 的「刚看完什么，接下来先给什么」反馈回路。
        self.last_click = None
        # 客户端点击事件与正文预取请求可能走不同的 HTTP worker；用
        # 会话内序号保护“最近”语义，避免旧请求晚到后把锚点覆盖回去。
        # 未带序号的调用方仍可退回时间戳比较。
        self.last_click_seq = None
        self.last_click_ts = -1
        self.click_history = []                   # 仅保留最近少量 ID，用于刷新时去重
        self.last_feed_ids = []                   # 最近一次返回给客户端的批次
        self.feed_history_ids = []                # 最近若干批次，刷新时避免立即重复
        # (mode, request_id) -> 已生成的完整 API 响应。只存在当前会话内存中，
        # 由服务端有界维护，用于响应丢失后的幂等重试。
        self.screen_response_cache = {}
        self.refresh_count = 0
        self.last_refresh_at = 0.0
        self.raw = defaultdict(float)             # 维度 -> 原始分
        self.evidence = defaultdict(dict)         # 维度 -> {content_id: 证据行}
        self.trait = {a: {"pro": 0.0, "con": 0.0} for a in AXES}
        self.trait_evidence = defaultdict(dict)   # 轴 -> {content_id: 证据行}
        self.impression_domains = defaultdict(int)
        self.skip_count = 0
        self.click_count = 0
        self.event_count = 0
        self.screen_index = 0
        self.cards_used = []                      # 离线调用使用的消歧轴
        self.pair_axes_seen = defaultdict(int)    # 已展示过的成对探索轴
        self.pending_card = None
        self.finished = False
        self.deleted = False
        self.show_on_screen = True
        self.feedback = None
        self._create_request_id = None    # 仅供 SessionStore 的内存幂等映射
        self._feedback_hook = None       # 由 SessionStore 注入；不参与任何输出
        self.hot = defaultdict(float)             # 大屏"当前热度"（仅视觉，§8.2）
        self.hot_ts = self.created_at
        self.history = []                         # 画像置信度趋势（仅展示）
        self.last_hit = None                      # 最近一次计入画像的行为（大屏"最近计入"，仅展示）

    # ------------------------------------------------ 事件入账（§8.8）

    def on_event(self, ev):
        with self.lock:
            return self._on_event_unlocked(ev)

    def _on_event_unlocked(self, ev):
        if self.deleted or not isinstance(ev, dict):
            return False
        seq = ev.get("seq")
        if seq is not None:
            try:
                hash(seq)
            except TypeError:
                return False
            if seq in self.seen_seq:              # §19.4 幂等
                return False
            self.seen_seq.add(seq)
        self.event_count += 1
        self.updated_at = time.time()

        etype = ev.get("type")
        props = ev.get("props") if isinstance(ev.get("props"), dict) else {}

        if etype == "session_start":
            return True
        if etype == "session_end":
            # 结算页只有在 result_payload 完整生成后才把 finished 置真；
            # 这里仍接收结束事件并更新时间，避免它因为没有 content_id
            # 被当成坏事件丢掉。这样结果生成失败时仍可安全重试。
            return True
        if etype == "screen_view":
            raw_screen = _finite_number(props.get("screen_index", 0), float("nan"))
            if not math.isfinite(raw_screen):
                return False
            screen_index = max(0, int(raw_screen))
            self.screen_index = max(self.screen_index, screen_index)
            return True
        if etype == "feed_refresh":
            # 刷新本身不改变画像分数，也不推进 screen_index；它只是一次新的
            # 推荐请求。服务端在发批次时已递增 refresh_count，这里只记作合法事件。
            return True
        if etype == "probe_choice":
            self._on_probe_choice(ev)
            return True
        if etype == "profile_feedback":
            # 反馈只对应已经生成的结果；在结果页之前收到的请求不能
            # 改写认可率，也不能让外部猜测会话编号后刷入统计。
            if not self.finished:
                return False
            value = props.get("value")
            if value in ("accurate", "inaccurate"):
                old_feedback = self.feedback
                self.feedback = value
                hook = getattr(self, "_feedback_hook", None)
                if hook is not None and old_feedback != value:
                    # SessionStore 注册的钩子只接收两个匿名枚举值；它不会
                    # 读取或保存会话对象，从而不会把个体字段带入长期文件。
                    hook(self, old_feedback, value)
                return True
            return False

        cid = ev.get("content_id")
        content = self.lib.contents.get(cid)
        if content is None:
            return False

        # 点击/回看先写入短期上下文，再按下方规则计分。即使本次打开的权重
        # 因幂等约束没有新增，下一批仍应知道用户刚刚打开了哪篇内容。
        if etype in ("content_click", "content_review", "content_return"):
            self._remember_click(content, ev, props)

        if etype == "content_impression":         # §8.8 曝光单独记账，不进画像分
            self.impression_domains[content["domain"]] += 1
            return True

        if etype == "content_skip":
            self.skip_count += 1
            w = -0.15
        elif etype == "content_click":
            w = 0.5
            self.click_count += 1
        elif etype == "content_dwell":
            w = dwell_weight(props.get("dwell_ms"), props.get("scroll_depth"),
                             content.get("body_len", "S"))
        elif etype in ("content_return", "content_review"):
            w = RETURN_BONUS
        else:
            return False
        if w == 0:
            return True

        # §8.4 约束 1：同一内容取最高权重，只补差值；返回查看是唯一可追加的行为
        if etype in ("content_return", "content_review"):
            if cid in self.returned:
                return True
            self.returned.add(cid)
            delta_w = RETURN_BONUS
        else:
            prev = self.applied_w.get(cid, 0.0)
            if abs(w) <= abs(prev):
                return True
            delta_w = w - prev
            self.applied_w[cid] = w

        factor = delta_w * position_factor(props.get("position", 0))
        action = self._action_label(etype, props)
        self._apply(content, factor, action,
                    _safe_event_timestamp(ev.get("ts")))
        self._snapshot()
        return True

    def _remember_click(self, content, ev, props):
        """记录最近点击，供 Feeder 做短期推荐上下文（不写入持久化）。"""
        cid = content["content_id"]
        ev = ev if isinstance(ev, dict) else {}
        props = props if isinstance(props, dict) else {}
        ts_supplied = ev.get("ts") is not None
        context_hint = bool(ev.get("_context_hint"))
        ts = _safe_event_timestamp(ev.get("ts"))
        seq = _context_seq(ev.get("seq"))
        # 正文 GET 是点击事件抵达前的“先到信号”，而事件/GET 又可能
        # 乱序到达。新请求必须比当前上下文更新；同一个 seq 的事件是
        # 同一次点击，不应再次改写最近内容。
        current_ts = _finite_number(getattr(self, "last_click_ts", -1), -1)
        current_seq = _context_seq(getattr(self, "last_click_seq", None))
        if current_ts >= 0:
            if seq is not None:
                # 一旦进入带序号的客户端链路，就以序号为准；这也能
                # 容忍未带序号请求使用了略超前的服务器时间。
                if current_seq is not None and seq <= current_seq:
                    return False
            elif context_hint and ts_supplied and ts <= current_ts:
                return False
        position = max(0, int(_finite_number(props.get("position", 0), 0)))
        rec = {
            "content_id": cid,
            "title": content.get("title", ""),
            "domain": content.get("domain", ""),
            "domain_cn": content.get("domain_cn", DOMAIN_CN.get(content.get("domain"), "")),
            "sub_tags": dict(content.get("sub_tags") or {}),
            "traits": dict(content.get("traits") or {}),
            "position": position,
            "feed": _safe_feed_name(props.get("feed")),
            "ts": ts,
        }
        self.last_click = rec
        # 没有序号的调用方不能把已有序号降级；其上下文仍按到达
        # 顺序兼容更新，后续带序号的请求会继续从已有序号往前判断。
        if seq is not None:
            self.last_click_seq = seq
        self.last_click_ts = ts
        self.click_history = [x for x in self.click_history if x != cid]
        self.click_history.append(cid)
        if len(self.click_history) > 12:
            del self.click_history[:-12]
        return True

    def remember_context(self, content_id, props=None, ts=None, seq=None):
        """登记一次正文打开的短期上下文，但不改变画像分数。

        手机端点击事件采用批量上报；正文 GET 往往会先抵达服务端，
        因而可以作为推荐锚点的低成本“先到信号”。真正的点击权重仍只
        在 :meth:`on_event` 收到 ``content_click``/``content_review`` 时
        入账，避免因为网络重试把分数加两次。``seq`` 用于在多个请求
        乱序时保留真正较新的打开动作。
        """
        with self.lock:
            if self.deleted:
                return False
            content = self.lib.contents.get(content_id)
            if content is None:
                return False
            props = props if isinstance(props, dict) else {}
            ev = {}
            ev["_context_hint"] = True
            if ts is not None:
                ev["ts"] = ts
            if seq is not None:
                ev["seq"] = seq
            self._remember_click(content, ev, props)
            self.updated_at = time.time()
            return True

    def _on_probe_choice(self, ev):
        """兼容离线仿真的消歧事件；手机端当前流程不会产生此事件。"""
        props = ev.get("props") or {}
        axis = props.get("axis")
        picked = props.get("picked")             # "pro" | "con"
        if axis not in AXES or picked not in ("pro", "con"):
            return
        if axis in self.cards_used:
            return
        self.cards_used.append(axis)
        ts = _safe_event_timestamp(ev.get("ts"))
        # 消歧卡文案来自离线事件；只接受当前内容库中的
        # prompt，未知文本一律回退，避免把任意输入带到画像/大屏。
        label = props.get("label")
        known_labels = {
            card[2] for card in getattr(self.lib, "disambig_cards", ())
            if isinstance(card, (tuple, list)) and len(card) >= 3
            and isinstance(card[2], str) and card[2].strip()
        }
        if label not in known_labels:
            label = "二选一"
        self.trait[axis][picked] += DISAMBIG_W_PICKED
        # 未选项 w = −0.5，按 §8.3 记入其对立极（即已选极）
        self.trait[axis][picked] += abs(DISAMBIG_W_UNPICKED)
        self.trait_evidence[axis]["card_" + axis] = ({
            "content_id": "card_" + axis, "title": label,
            "action": "主动选择", "domain": None, "domain_cn": "消歧卡",
            "pole": picked, "contribution": DISAMBIG_W_PICKED + 0.5, "ts": ts})
        self.pending_card = None

    def _snapshot(self):
        """记一条「画像置信度趋势」采样点，纯展示用，不参与任何评分"""
        if len(self.history) < 80:
            self.history.append({"t": round(time.time() - self.created_at, 1),
                                 "s": round(self.top_domain()[1], 3)})

    @staticmethod
    def _merge_evidence(store, dim, cid, rec):
        """证据按内容去重（§8.4 约束 1 / §8.8）。

        raw 分只累加差值，但如果证据面板给「点击」和「停留 15 秒」各列一行，
        观众会看到同一条内容并排出现两次，立刻判定系统在重复计数 —— 分数是对的，
        可信度照样归零。因此这里按 content_id 合并：贡献累加，动作取最新一次
        （delta 只在信号更强时产生，最新即最强）。n_evidence 也因此是真实的
        「几条内容」，大屏敢打出"只用了 3 次点击"。
        """
        cur = store[dim].get(cid)
        if cur is None or cur.get("pole") != rec.get("pole"):
            store[dim][cid] = rec               # 极性翻转（先划走后点开）以最新为准
        else:
            cur["contribution"] += rec["contribution"]
            cur["action"] = rec["action"]
            cur["ts"] = rec["ts"]

    def _apply(self, content, factor, action, ts):
        cid = content["content_id"]
        # ---- Layer 1：只有正信号进兴趣分（§8.8 决策说明）
        if factor > 0:
            for tag, tw in content["sub_tags"].items():
                self.raw[tag] += factor * tw
                self._merge_evidence(self.evidence, tag, cid, {
                    "content_id": cid, "title": content["title"],
                    "action": action, "contribution": factor * tw, "ts": ts,
                    "domain": content["domain"]})
            d = content["domain"]
            self.raw[d] += factor * DOMAIN_W
            self._merge_evidence(self.evidence, d, cid, {
                "content_id": cid, "title": content["title"],
                "action": action, "contribution": factor * DOMAIN_W, "ts": ts,
                "domain": d})
            self._decay_hot()
            self.hot[content["domain"]] += factor
            self.last_hit = {"content_id": cid, "title": content["title"], "action": action,
                             "domain": content["domain"],
                             "domain_cn": DOMAIN_CN.get(content["domain"], ""),
                             "theme": content.get("cover_theme", "tech"),
                             "delta": round(factor, 3), "ts": ts}

        # ---- Layer 2：负信号记入对立极（§8.3）
        for trait, tw in content["traits"].items():
            axis, pole = TRAIT_AXIS[trait]
            bucket = pole if factor > 0 else OPPOSITE[pole]
            contrib = abs(factor) * tw
            self.trait[axis][bucket] += contrib
            self._merge_evidence(self.trait_evidence, axis, cid, {
                "content_id": cid, "title": content["title"], "action": action,
                "domain": content["domain"],
                "domain_cn": DOMAIN_CN.get(content["domain"], ""),
                "pole": bucket, "contribution": contrib, "ts": ts})

    @staticmethod
    def _action_label(etype, props):
        if etype == "content_click":
            return "点击"
        if etype == "content_dwell":
            dwell_ms = max(0.0, min(_finite_number(props.get("dwell_ms", 0), 0), DWELL_CAP_MS))
            return "停留 %d 秒" % round(dwell_ms / 1000)
        if etype in ("content_return", "content_review"):
            return "返回查看"
        if etype == "content_skip":
            return "快速划过"
        return etype

    def _decay_hot(self):
        """大屏当前热度：T_half = 20s，纯视觉，不影响任何分数（§8.2）"""
        now = time.time()
        dt = now - self.hot_ts
        if dt > 0:
            f = 0.5 ** (dt / 20.0)
            for k in list(self.hot):
                self.hot[k] *= f
        self.hot_ts = now

    # ------------------------------------------------ 评分（§8.2/8.3/8.6）

    def score(self, dim):
        return 1.0 - math.exp(-self.raw[dim] / KAPPA)

    def conf(self, dim):
        return min(1.0, len(self.evidence[dim]) / CONF_N)

    def domain_scores(self):
        out = {}
        for d in DOMAIN_CN:
            if self.raw[d] > 0:
                out[d] = {"score": round(self.score(d), 4),
                          "conf": round(self.conf(d), 3),
                          "n_evidence": len(self.evidence[d]),
                          "cn": DOMAIN_CN[d]}
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["score"]))

    def subtag_scores(self, limit=6):
        out = {}
        for k, v in self.raw.items():
            if k in DOMAIN_CN or v <= 0:
                continue
            out[k] = {"score": round(self.score(k), 4),
                      "conf": round(self.conf(k), 3),
                      "n_evidence": len(self.evidence[k]),
                      "cn": cn(k)}
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["score"])[:limit])

    def trait_scores(self):
        out = {}
        for axis in AXES:
            pro, con = self.trait[axis]["pro"], self.trait[axis]["con"]
            value = (1.0 + pro) / (2.0 + pro + con)      # Beta 后验均值，α=β=1
            n = len(self.trait_evidence[axis])
            has_card = any(str(k).startswith("card_") for k in self.trait_evidence[axis])
            conf = min(1.0, (n + (1 if has_card else 0)) / CONF_N)
            if value > NEUTRAL_HI:
                pole = POLE_NAME[axis][0]
            elif value < NEUTRAL_LO:
                pole = POLE_NAME[axis][1]
            else:
                pole = None                              # 中性区不展示
            if pole and conf < 0.5:
                pole = None
            doms = {e["domain"] for e in self.trait_evidence[axis].values() if e["domain"]}
            out[axis] = {"value": round(value, 4), "pole": pole,
                         "pole_cn": TRAIT_CN.get(pole), "n": n,
                         "axis_cn": AXIS_CN[axis], "conf": round(conf, 3),
                         "cross_domain": len(doms) >= 3}
        return out

    def top_domain(self):
        ds = self.domain_scores()
        if not ds:
            return None, 0.0, 0.0
        d, v = next(iter(ds.items()))
        return d, v["score"], v["conf"]

    def converged(self):
        """收敛判据：主兴趣域证据充足，且至少两个特质轴脱离中性区。"""
        d, s, c = self.top_domain()
        if not d or s < CONVERGE_SCORE or c < 1.0:
            return False
        off = sum(1 for t in self.trait_scores().values() if t["pole"])
        return off >= 2

    # ------------------------------------------------ Evidence（§10）

    def _theme(self, cid):
        c = self.lib.contents.get(cid)
        return c.get("cover_theme", "tech") if c else "tech"

    def evidence_for(self, dim, limit=3):
        total = sum(e["contribution"] for e in self.evidence[dim].values()) or 1.0
        items = sorted(self.evidence[dim].values(), key=lambda e: -e["contribution"])[:limit]
        return [{"content_id": e["content_id"], "title": e["title"],
                 "action": e["action"], "domain_cn": DOMAIN_CN.get(e["domain"], ""),
                 "theme": self._theme(e["content_id"]),
                 "contribution": round(e["contribution"] / total, 3)} for e in items]

    def trait_evidence_for(self, axis, limit=3):
        """§10.3 跨域证据：优先挑来自不同兴趣域的三条"""
        evs = sorted(self.trait_evidence[axis].values(), key=lambda e: -e["contribution"])
        picked, used, ids = [], set(), set()
        for e in evs:                       # 先每域取一条，跨域感最强
            if e["domain"] in used:
                continue
            used.add(e["domain"])
            ids.add(e["content_id"])
            picked.append(e)
            if len(picked) >= limit:
                break
        for e in evs:
            if len(picked) >= limit:
                break
            if e["content_id"] not in ids:
                ids.add(e["content_id"])
                picked.append(e)
        return [{"content_id": e["content_id"], "title": e["title"],
                 "action": e["action"], "domain_cn": e["domain_cn"],
                 "theme": self._theme(e["content_id"]),
                 "pole_cn": TRAIT_CN.get(POLE_NAME[axis][0 if e["pole"] == "pro" else 1]),
                 } for e in picked]

    # ------------------------------------------------ 画像输出（§8.7）

    def profile(self):
        domains = self.domain_scores()
        traits = self.trait_scores()
        top_d, top_s, _ = self.top_domain()
        return {
            "session_id": self.sid, "codename": self.codename,
            "updated_at": int(self.updated_at * 1000),
            "event_count": self.event_count, "click_count": self.click_count,
            "skip_count": self.skip_count,
            "impression_count": sum(self.impression_domains.values()),
            "screen_index": self.screen_index,
            "domains": domains, "sub_tags": self.subtag_scores(),
            "traits": traits,
            "hot": {k: round(v, 3) for k, v in self.hot.items() if v > 0.01},
            "history": self.history,
            "last_hit": self.last_hit,
            # 只返回内容库中本来就会展示的字段，不暴露设备或身份信息。
            "last_click": ({k: v for k, v in self.last_click.items()
                            if k not in ("sub_tags", "traits")} if self.last_click else None),
            "refresh_count": self.refresh_count,
            "persona": self.persona(),
            "converged": self.converged(),
            "finished": self.finished,
            "top_domain": top_d, "top_score": round(top_s, 4),
        }

    def persona(self):
        """生成结果页的短标签。只描述本次浏览行为，不做人格定论。"""
        d, _, dconf = self.top_domain()
        if not d:
            return "本次浏览信号很少"
        if dconf < 0.5:
            return "本次浏览信号有限"
        if dconf < 1.0:
            return "%s兴趣信号" % DOMAIN_CN[d]
        traits = self.trait_scores()
        # 主导特质 = 偏离中性最远、且脱离中性区的那一条
        best, best_dev = None, 0.0
        for axis, t in traits.items():
            if not t["pole"]:
                continue
            dev = abs(t["value"] - 0.5)
            if dev > best_dev:
                best, best_dev = t["pole"], dev
        if best is None:
            n_domains = len(self.domain_scores())
            if n_domains >= 5:
                return "本次浏览呈现广谱兴趣"  # §5.4 边缘兜底
            return PERSONA_FALLBACK_DOMAIN.get(d, "内容浏览者")
        return "%s · %s倾向" % (DOMAIN_CN[d], TRAIT_CN[best])

    def summary_text(self):
        """§12.4 模板文案（现场大多数人实际看到的版本，必须写得够好）"""
        d, s, dconf = self.top_domain()
        traits = self.trait_scores()
        cross = [t for t in traits.values() if t["pole"] and t["cross_domain"]]
        strong = [t for t in traits.values() if t["pole"]]
        if not d:
            return "你几乎什么都没点——但你划过了 %d 条内容，这本身也是数据。" % \
                   sum(self.impression_domains.values())
        if dconf < 1.0:
            return "基于本次浏览，系统暂时看到你对「%s」有一些兴趣信号，但证据还不充分。" % DOMAIN_CN[d]
        if cross:
            t = max(cross, key=lambda x: abs(x["value"] - 0.5))
            return "你在「%s」上表现出明显兴趣，并且在多个不相关的板块中，都体现出%s的倾向。" % \
                   (DOMAIN_CN[d], t["pole_cn"])
        if strong:
            t = max(strong, key=lambda x: abs(x["value"] - 0.5))
            return "你在「%s」上表现出明显兴趣，阅读方式偏向%s。" % (DOMAIN_CN[d], t["pole_cn"])
        return "你在「%s」上表现出明显兴趣，只用了 %d 次点击。" % (DOMAIN_CN[d], self.click_count)


# ================================================================ 投放（§9 / §11）

class Feeder:
    def __init__(self, lib):
        self.lib = lib

    def build_screen(self, s, n=SCREEN_SIZE, anchor_id=None, anchor_seq=None):
        """取下一屏推荐内容，并把最近点击作为短期推荐上下文。"""
        try:
            n = max(0, int(n))
        except (TypeError, ValueError):
            n = SCREEN_SIZE
        with s.lock:
            anchor = self._anchor_for(s, anchor_id, anchor_seq)
            # 正常流程的第一屏仍是八域正交探针；但如果观众在首屏
            # 请求失败、或先从「发现」栏打开了内容，已有的最近点击
            # 就是更强的短期上下文，不能被冷启动探针覆盖掉。
            if n == 0:
                cards = []
            elif s.screen_index == 0 and not s.seen and not anchor:
                cards = self._probe_screen(s)
            else:
                # 发现栏的卡片不会写入 ``s.seen``。若用户刚从发现栏点开
                # 一篇内容，默认 feed_pool 过滤仍会把它当作“未出现”，下一
                # 屏可能原样再投一次；锚点本身始终不应出现在紧接着的批次。
                pool = [c for c in self.lib.feed_pool
                        if c["content_id"] not in s.seen and
                        (not anchor or c["content_id"] != anchor.get("content_id"))]
                cards = self._mixed_screen(s, n, pool=pool, anchor=anchor)
            self._record_batch(s, cards)
            return cards

    def build_refresh(self, s, n=SCREEN_SIZE, anchor_id=None, anchor_seq=None):
        """生成一次下拉刷新批次，不推进体验屏序。"""
        try:
            n = max(0, int(n))
        except (TypeError, ValueError):
            n = SCREEN_SIZE
        with s.lock:
            anchor = self._anchor_for(s, anchor_id, anchor_seq)
            anchor_cid = anchor.get("content_id") if anchor else None
            # 发现栏不占用首页 seen；同一篇刚在发现栏打开后也要被视为
            # “当前锚点”，不能因为它尚未进入首页历史就立刻刷新回来。
            unseen = [c for c in self.lib.feed_pool
                      if c["content_id"] not in s.seen and
                      c["content_id"] != anchor_cid]
            recent = set(s.feed_history_ids[-48:] or s.last_feed_ids)
            recycled = [c for c in self.lib.feed_pool
                        if c["content_id"] not in recent
                        and c["content_id"] != anchor_cid]
            # 只要还有未出现过的素材，刷新就只从这部分取。将 unseen 与
            # recycled 合并后再排序，兴趣分高的旧卡可能抢在新卡前面，用户会
            # 觉得“刷新”只是把刚才看过的内容重新洗牌。素材真正耗尽后才允许
            # 轮换较早批次；这样也让连续下拉的行为更接近真实信息流。
            pool = unseen if unseen else recycled
            if not pool:
                # 素材耗尽时允许轮换旧卡；先避开刚返回的整批，再退一步
                # 才允许重复。无论哪一步都排除当前锚点，避免刷新后立刻
                # 看到同一篇。
                last_ids = set(s.last_feed_ids)
                pool = [c for c in self.lib.feed_pool
                        if c["content_id"] not in last_ids and
                        c["content_id"] != anchor_cid]
                if not pool:
                    pool = [c for c in self.lib.feed_pool if c["content_id"] != anchor_cid]
            cards = self._mixed_screen(s, n, pool=pool, anchor=anchor)
            if not unseen and len(cards) < n:
                # 已确认没有任何未出现素材后，轮换池可能仍因“避开最近
                # 一批”而不足一整屏；此时才允许从更近的旧卡补齐。
                used = {c["content_id"] for c in cards}
                extras = [c for c in self.lib.feed_pool
                          if c["content_id"] not in used and c["content_id"] != anchor_cid]
                extras.sort(key=lambda c: -(self._recommendation_score(c, s, anchor) +
                                            0.35 * self._explore_score(c, s) + random.random() * 1e-3))
                cards.extend(extras[:max(0, n - len(cards))])
            # 只有真正返回了一批内容才算一次成功刷新。素材临时为空时，
            # 客户端会进入可重试状态；不应让 refresh_count 看起来像已经
            # 换过一批，也不应把空批次写进最近批次历史。
            if cards:
                self._record_batch(s, cards)
                s.refresh_count += 1
                s.last_refresh_at = time.time()
            return cards

    @staticmethod
    def _record_batch(s, cards):
        ids = [c["content_id"] for c in cards]
        if ids:
            # 投放本身也是会话活动：大屏的活跃排序、后台“最近在线”列表
            # 不应只在行为事件到达时才更新。事件仍由 on_event 负责计数，
            # 这里只刷新会话活跃时间。
            s.updated_at = time.time()
            s.seen.update(ids)
            s.last_feed_ids = ids
            s.feed_history_ids.extend(ids)
            if len(s.feed_history_ids) > 96:
                del s.feed_history_ids[:-96]

    def _anchor_for(self, s, anchor_id=None, anchor_seq=None):
        """解析最近点击；客户端提示只能指向本会话已见或已打开的内容。

        ``anchor_seq`` 是手机端点击事件的同一序号。若一个较早的屏幕请求
        晚于新点击才到达，就回退到会话里已确认的最新内容；没有序号的
        未带序号的调用方仍按原来的 ID 校验与回退规则工作。
        """
        click_history = getattr(s, "click_history", ()) or ()
        # 先接受客户端带来的最新锚点。点击事件采用批量上报，网络抖动时
        # `/api/screen` 可能早于 `/api/events` 到达；若此处始终优先读旧的
        # last_click，刷新会跟随最近一次点击，而不是更早的内容。
        if isinstance(anchor_id, str):
            anchor_id = anchor_id.strip()
        anchor_seq_supplied = anchor_seq is not None
        candidate_seq = _context_seq(anchor_seq)
        latest_seq = _context_seq(getattr(s, "last_click_seq", None))
        latest = s.last_click if isinstance(s.last_click, dict) else None
        latest_cid = latest.get("content_id") if latest else None
        if not isinstance(latest_cid, str):
            latest_cid = None
        if isinstance(anchor_id, str) and anchor_id:
            c = self.lib.contents.get(anchor_id)
            # 首页投放会写入 seen；发现栏点击不占首页 seen，但会写入
            # click_history。两者都属于本会话已实际见过/打开的内容，
            # 允许客户端在事件批量到达前提供这个短暂锚点。
            if c and (anchor_id in s.seen or anchor_id in click_history):
                # 同一会话可能同时有几个屏幕/正文请求在飞行。带序号的调用方
                # 会带 click seq；若请求携带的是更早的锚点，必须回退到
                # 服务端已经确认的最近点击，避免旧响应反向改写推荐。
                stale = False
                if candidate_seq is not None and latest_seq is not None:
                    stale = candidate_seq < latest_seq or (
                        candidate_seq == latest_seq and latest_cid and latest_cid != anchor_id)
                elif anchor_seq_supplied and candidate_seq is None and latest_seq is not None:
                    stale = bool(latest_cid and latest_cid != anchor_id)
                if not stale:
                    return c
        if latest:
            c = self.lib.contents.get(latest_cid) if latest_cid else None
            if c:
                return c
        # 兼容状态恢复或只保留 click_history 的情况；按最近到最早
        # 回退，避免因为一条已从库中删除的 ID 丢掉全部短期上下文。
        for cid in reversed(list(click_history)):
            if not isinstance(cid, str):
                continue
            c = self.lib.contents.get(cid)
            if c:
                return c
        return None

    def anchor_info(self, s, anchor_id=None, anchor_seq=None):
        """返回可安全放进 API 响应的推荐锚点摘要。"""
        with s.lock:
            c = self._anchor_for(s, anchor_id, anchor_seq)
            if not c:
                return None
            return {"content_id": c["content_id"],
                    "title": c.get("title", ""),
                    "domain": c.get("domain", ""),
                    "domain_cn": c.get("domain_cn", DOMAIN_CN.get(c.get("domain"), ""))}

    # ---- 第 1 屏：正交探针，覆盖 8 个互不相交的学生兴趣域（§5.1）
    def _probe_screen(self, s):
        probe_ids = PROBE_GROUPS.get(s.probe_group, PROBE_GROUPS["default"])
        by_id = {c["content_id"]: c for c in self.lib.probes}
        picked, picked_ids = [], set()
        for cid in probe_ids:
            if cid in by_id and cid not in s.seen and cid not in picked_ids:
                picked.append(by_id[cid]); picked_ids.add(cid)
        # 临时删掉某条探针时，仍尽量补齐缺失域，避免首屏塌缩。
        covered = {c["domain"] for c in picked}
        if len(picked) < len(probe_ids):
            for c in self.lib.probes:
                if (c["content_id"] in s.seen or c["content_id"] in picked_ids or
                        c["domain"] in covered):
                    continue
                picked.append(c)
                picked_ids.add(c["content_id"])
                covered.add(c["domain"])
                if len(picked) >= len(probe_ids):
                    break
        random.shuffle(picked)
        return picked

    # ---- 第 2 屏起：位 1-4 兴趣位，位 5-6 成对探索位（§11.2）
    def _mixed_screen(self, s, n, pool=None, anchor=None):
        try:
            n = max(0, int(n))
        except (TypeError, ValueError):
            n = SCREEN_SIZE
        if n <= 0:
            return []
        if pool is None:
            pool = [c for c in self.lib.feed_pool if c["content_id"] not in s.seen]
        else:
            pool = list(pool)
        # 调用方通常传入库中的唯一列表；状态恢复/外部适配器若带来重复
        # 卡片，先在引擎侧去重，保证“同一批不重复”不依赖前端兜底。
        unique_pool, pool_ids = [], set()
        for c in pool:
            if not isinstance(c, dict):
                continue
            cid = c.get("content_id")
            if not isinstance(cid, str) or not cid or cid in pool_ids:
                continue
            pool_ids.add(cid)
            unique_pool.append(c)
        pool = unique_pool
        # 无论调用方传入的是“未曝光”还是轮换池，都不把刚点开的同一
        # 张卡作为下一批候选。尤其是发现栏点击不写入 s.seen，这个过滤
        # 是防止原卡立刻回流的最后一道保险。
        if anchor:
            anchor_cid = anchor.get("content_id")
            if anchor_cid:
                pool = [c for c in pool if c.get("content_id") != anchor_cid]
        if not pool:
            return []
        st = {"tag": defaultdict(int), "dom": defaultdict(int), "ids": set()}
        ranked = list(s.domain_scores())
        by_interest = lambda c: -(self._recommendation_score(c, s, anchor) + random.random() * 1e-3)
        by_explore = lambda c: -(self._explore_score(c, s) +
                                  0.35 * self._anchor_score(c, anchor) + random.random() * 1e-3)
        out = []

        # 「刚看完什么，接下来先给什么」只占一个兴趣位，剩余位置仍保留
        # 当前画像和探索位，避免一次误点把整条信息流锁死。
        if anchor:
            related = [c for c in pool if self._anchor_score(c, anchor) > 0]
            out += self._take(s, related, min(1, n), st, by_interest)

        # 位 1-2：当前 top1 域，巩固主线，给用户"它懂我"的感觉。
        # 但 top1 证据已满（conf=1）后只保留 1 条：raw(d) 是求和式（§8.2），
        # top1 每屏还独占 2 个成对探索位，再给满 2 个兴趣位就会滚雪球——
        # 首屏猜错的域会被投放自我强化，正是 §8.5 想避免的回音室，只是发生在屏间。
        interest_limit = min(4, n)
        if ranked and len(out) < interest_limit:
            keep = 1 if s.conf(ranked[0]) >= 1.0 else 2
            keep = min(keep, interest_limit - len(out))
            out += self._take(s, [c for c in pool if c["domain"] == ranked[0]],
                              keep, st, by_interest)
        # 位 3：优先补**从未曝光过的域**。首屏按方案应覆盖 8 域，但内容库或探针
        # 不完整时这里兜底，避免某个域永远进不了后续屏。
        # 只留 1 个位：占满 2 个会挤掉 top2/top3 的对照位。
        # 状态恢复或内容库热更新时，seen 里可能残留已不存在的 ID；
        # 这不应让一次推荐请求因 KeyError 整体失败。
        shown = {c["domain"] for cid in s.seen
                 for c in [self.lib.contents.get(cid)] if c and c.get("domain")}
        unseen_dom = [d for d in DOMAIN_CN if d not in shown]
        if unseen_dom and len(out) < interest_limit:
            out += self._take(s, [c for c in pool if c["domain"] in unseen_dom],
                              min(1, interest_limit - len(out)), st, by_explore)
        # 位 3-4 余下：top2/top3 域
        if len(out) < interest_limit:
            out += self._take(s, [c for c in pool if c["domain"] in ranked[1:3]],
                              interest_limit - len(out), st, by_interest)
        if len(out) < interest_limit:                        # 兜底：约束放宽
            out += self._take(s, pool, interest_limit - len(out), st, by_interest, relax=True)

        # 位 5-6：成对探索位。§9.3 铁律要求成对内容必须同域且落在 top1 域内，
        # 与 §11.3「同域单屏 ≤3 条」直接冲突 —— 以 §9.3 为准，该上限只约束兴趣位。
        explore = self._pick_pair(s, [c for c in pool if c["content_id"] not in st["ids"]], anchor=anchor)
        result = out + explore
        if len(result) < n:
            # 小内容库、连续刷新或严格标签约束下，候选可能不足以填满
            # 一屏。最后放宽展示约束补齐剩余位置；仍保持去重与锚点排除，
            # 避免真实客户端把“合法但短的批次”误判成加载失败。
            used = {c["content_id"] for c in result}
            extras = [c for c in pool
                      if c["content_id"] not in used and
                      (not anchor or c["content_id"] != anchor.get("content_id"))]
            extras.sort(key=lambda c: -(self._recommendation_score(c, s, anchor) +
                                        0.35 * self._explore_score(c, s) + random.random() * 1e-3))
            result.extend(extras[:max(0, n - len(result))])
        return result[:n]

    def _take(self, s, cands, k, st, key, relax=False):
        """按 §11.3 去重与新鲜度约束，从候选里挑 k 条"""
        out = []
        if k <= 0:
            return out
        for c in sorted(cands, key=key):
            cid = c["content_id"]
            if cid in st["ids"]:
                continue
            tags = list(c["sub_tags"]) or [c["domain"]]
            if not relax:
                if st["dom"][c["domain"]] >= MAX_PER_DOMAIN:
                    continue
                if any(st["tag"][t] >= MAX_PER_SUBTAG for t in tags):
                    continue
            out.append(c)
            st["ids"].add(cid)
            st["dom"][c["domain"]] += 1
            for t in tags:
                st["tag"][t] += 1
            if len(out) >= k:
                break
        return out

    def _interest_score(self, c, s):
        v = s.score(c["domain"]) * DOMAIN_W
        for tag, tw in c["sub_tags"].items():
            v += s.score(tag) * tw
        return v

    def _anchor_score(self, c, anchor):
        """候选与最近点击内容的短期相关性，只用于排序加成。"""
        if not anchor or c["content_id"] == anchor.get("content_id"):
            return 0.0
        v = 1.0 if c.get("domain") == anchor.get("domain") else 0.0
        atags = anchor.get("sub_tags") or {}
        for tag, weight in (c.get("sub_tags") or {}).items():
            if tag in atags:
                try:
                    v += 0.8 * min(float(weight or 0), float(atags.get(tag) or 0))
                except (TypeError, ValueError):
                    pass
        # 共享同一特质轴算弱相关，但不让特质标签压过兴趣域。
        axes_a = {TRAIT_AXIS[t][0] for t in (anchor.get("traits") or {}) if t in TRAIT_AXIS}
        axes_c = {TRAIT_AXIS[t][0] for t in (c.get("traits") or {}) if t in TRAIT_AXIS}
        v += 0.2 * len(axes_a & axes_c)
        return v

    def _recommendation_score(self, c, s, anchor=None):
        # 画像分仍占主导；一次点击只提供有限的短期加成。
        return self._interest_score(c, s) + 1.35 * self._anchor_score(c, anchor)

    def _gap(self, s, d):
        """§9.2 gap(d) = 1 − |score(d) − score(d')|，d' 为分数最接近的其他维度"""
        scores = {k: s.score(k) for k in s.raw if s.raw[k] > 0}
        if d not in scores or len(scores) < 2:
            return 1.0
        me = scores[d]
        others = [abs(me - v) for k, v in scores.items() if k != d]
        return 1.0 - min(others)

    def _explore_score(self, c, s):
        purity = c.get("purity") or 0.3
        v = 0.0
        for d, tw in list(c["sub_tags"].items()) + [(c["domain"], DOMAIN_W)]:
            v += tw * (1.0 - s.conf(d)) * purity * self._gap(s, d)
        return v

    def _pick_pair(self, s, pool, anchor=None):
        """§9.3 成对投放：同域 + 同轴对立。top1 证据足够后才收窄到 top1。"""
        avail = {c["content_id"] for c in pool}
        ranked = list(s.domain_scores())
        top_d = ranked[0] if ranked else None
        top_conf = s.conf(top_d) if top_d else 0.0
        if top_d and top_conf >= 1.0:
            domains = [top_d]
        else:
            anchor_domain = anchor.get("domain") if anchor else None
            domains = ([anchor_domain] if anchor_domain else []) + ranked[:3] + \
                      sorted(DOMAIN_CN, key=lambda d: (s.conf(d), -s.score(d)))
        domains = [d for d in dict.fromkeys([x for x in domains if x])]

        cands = []
        for d in domains:
            cands += [(d, axis, a, b) for axis, a, b in self.lib.pairs.get(d, [])
                      if a in avail and b in avail]
        if cands:
            traits = s.trait_scores()

            def pair_score(p):
                d, axis, a, b = p
                ca, cb = self.lib.contents[a], self.lib.contents[b]
                # 早期不要被首个点击锁死：轴的不确定性、域证据缺口、内容探索分一起比较。
                uncertainty = 1.0 - abs(traits[axis]["value"] - 0.5) * 2
                trait_need = 1.0 - traits[axis]["conf"]
                domain_need = 1.0 - s.conf(d)
                domain_fit = s.score(d)
                freshness = 1.0 / (1.0 + s.pair_axes_seen[axis])
                explore = self._explore_score(ca, s) + self._explore_score(cb, s)
                top_bonus = 0.25 if d == top_d else 0.0
                anchor_bonus = 0.45 if anchor and d == anchor.get("domain") else 0.0
                purity = self.lib.pair_purity.get((d, axis, a, b), 0.5)
                return (explore + 1.1 * trait_need + 0.7 * uncertainty +
                        0.55 * domain_need + 0.35 * domain_fit + 0.3 * freshness +
                        0.25 * purity + top_bonus + anchor_bonus)

            d, axis, a, b = max(cands, key=pair_score)
            s.pair_axes_seen[axis] += 1
            two = [self.lib.contents[a], self.lib.contents[b]]
            random.shuffle(two)
            return two
        # 兜底：无可用配对时取 explore 分 top-2
        return sorted(pool, key=lambda c: -(self._explore_score(c, s) +
                                             0.35 * self._anchor_score(c, anchor)))[:2]

    # ---- 离线调用的消歧卡接口；手机端结束流程不再调用
    def next_card(self, s):
        """取最接近中性、证据最少的轴，且不与已用轴重复；每场最多 2 张"""
        if len(s.cards_used) >= 2:
            return None
        traits = s.trait_scores()
        cands = [a for a in AXES if a not in s.cards_used]
        if not cands:
            return None
        axis = min(cands, key=lambda a: (abs(traits[a]["value"] - 0.5),
                                         len(s.trait_evidence[a])))
        cards = getattr(self.lib, "disambig_cards", DISAMBIG_CARDS)
        card = random.choice([c for c in cards if c[1] == axis])
        qid, axis, prompt, a_text, b_text = card
        payload = {"card_id": qid, "axis": axis, "axis_cn": AXIS_CN[axis],
                   "prompt": prompt,
                   "options": [{"pole": "pro", "text": a_text,
                                "trait_cn": TRAIT_CN[POLE_NAME[axis][0]]},
                               {"pole": "con", "text": b_text,
                                "trait_cn": TRAIT_CN[POLE_NAME[axis][1]]}]}
        s.pending_card = payload
        return payload

# ================================================================ 广告匹配（§14.3）

def profile_vector(s):
    """把画像摊平成 {维度: 0~1}，供广告定向比对"""
    vec = {}
    for d in DOMAIN_CN:
        if s.raw[d] > 0:
            vec[d] = s.score(d)
    for k, v in s.raw.items():
        if k not in DOMAIN_CN and v > 0:
            vec[k] = s.score(k)
    for axis, t in s.trait_scores().items():
        pro, con = POLE_NAME[axis]
        vec[pro] = t["value"]
        vec[con] = 1.0 - t["value"]
    return vec


def match_ads(s, lib, top=1):
    try:
        top = max(0, int(top))
    except (TypeError, ValueError):
        top = 1
    if top == 0:
        return []
    vec = profile_vector(s)
    scored = []
    for ad in lib.ads:
        if not isinstance(ad, dict):
            continue
        ad_id, ad_title, ad_body = ad.get("ad_id"), ad.get("title"), ad.get("body")
        if not all(isinstance(x, str) and x.strip() for x in (ad_id, ad_title, ad_body)):
            continue
        targeting_data = ad.get("targeting")
        if not isinstance(targeting_data, dict):
            continue
        targeting = {}
        for group in ("domains", "sub_tags", "traits"):
            group_data = targeting_data.get(group) or {}
            if not isinstance(group_data, dict):
                continue
            for key, weight in group_data.items():
                try:
                    weight = float(weight)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(weight) and weight > 0:
                    targeting[str(key)] = weight
        if not targeting:
            continue
        num = sum(min(vec.get(d, 0.0), w) for d, w in targeting.items())
        den = sum(targeting.values())
        hits = sum(1 for d, w in targeting.items() if vec.get(d, 0.0) >= 0.5 * w)
        coverage = min(1.0, hits / 3.0)          # §14.3：v3 修正项，不加则公式是坏的
        # 域相关性因子（本实现补）。§14.3 的 num/den 在定向权重被打满时一律饱和到
        # 1.0，于是"只定向 tech 0.5 + 两条近乎人人都有的特质"的广告，会稳定压过
        # 定向精确的同域广告 —— 现场表现为「旅行 68% 的观众收到技术书广告」，
        # 那句"为什么给你看这条"当场就不成立了。乘上画像在被定向域上的实际分数，
        # 广告域与 top1 域一致率从 80% 升到 95%（simulate.py 实测）。
        doms = targeting_data.get("domains") or {}
        if not isinstance(doms, dict):
            doms = {}
        relevance = max([vec.get(d, 0.0) for d in doms], default=0.5)
        align = sum(vec.get(d, 0.0) for d in targeting)   # 平手时的次级排序键
        scored.append((num / den * coverage * relevance, align, hits, ad))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out = []
    # 先填理由再截取 top：最高匹配广告可能没有任何可追溯证据，
    # 这种广告宁可不投，也不能让空理由卡片占住唯一的展示位。
    for score, align, hits, ad in scored:
        if score <= 0:
            continue
        reasons = fill_reasons(ad, s, vec)
        if not reasons:
            continue
        out.append({"ad_id": ad["ad_id"], "title": ad["title"], "body": ad["body"],
                    "is_simulated": True, "match": round(score, 3), "hits": hits,
                    "reasons": reasons})
        if len(out) >= top:
            break
    return out


def fill_reasons(ad, s, vec):
    """填充 reason_template，白盒可追溯（内容库设计 §10.3 / 改进建议 D.15）。

    模板语义（每行独立门控，取不到值宁缺毋滥）：
      - {trait:xxx}    行门控：该具体特质当前被展示才保留整行，否则丢弃
      - {cross:axis}   行门控：该轴有跨板块证据（cross_domain）才保留整行
      - {n_xxx}        计数占位：该子标签/域的"正向证据内容数"，为 0 时丢弃整行
      - {top_content} / {dwell}：真实停留证据；没有真实停留时丢弃该行
    无任何占位的整句断言一律视为不可追溯，丢弃。
    """
    if not isinstance(ad, dict) or s is None:
        return []
    ts = s.trait_scores()
    # `trait:` 占位符写的是具体特质名（例如 deep_reader），而
    # `trait_scores()` 为了表达轴的正/负极只返回 ``pole=pro|con``。
    # 直接拿 pole 字符串去比会让所有特质理由都被错误丢弃；先把轴极性
    # 还原成 taxonomy 中的特质键，再做行门控。
    active_traits = {
        POLE_NAME[axis][0 if value.get("pole") == "pro" else 1]
        for axis, value in ts.items()
        if value.get("pole") in ("pro", "con") and axis in POLE_NAME
    }
    # 跨域证据本身不等于已经判定某一极；只有轴也脱离中性区时，
    # 才能支持“多个板块都体现出……”这类理由。
    cross_axes = {axis for axis, t in ts.items()
                  if t.get("cross_domain") and t.get("pole") and axis in POLE_NAME}

    # 证据行（仅正信号，含点击/停留/返回）；停留行单独取
    all_ev = [e for lst in s.evidence.values() for e in lst.values()]
    dwell_ev = [e for e in all_ev if str(e.get("action", "")).startswith("停留")]
    dwell_top = max(dwell_ev, key=lambda e: e["contribution"]) if dwell_ev else None
    dwell_sec = 0
    if dwell_top:
        m = re.search(r"(\d+)", str(dwell_top.get("action", "")))
        dwell_sec = int(m.group(1)) if m else 0

    def count_for(key):
        n = 0
        # applied_w 记录常规点击/停留的最高权重，但“返回查看”是可追加
        # 的独立正信号，不一定会写入 applied_w。以 Evidence 中实际存在
        # 的正贡献 ID 为主，同时兼容旧会话只保留 applied_w 的状态。
        positive_ids = {cid for cid, weight in s.applied_w.items() if weight > 0}
        for entries in s.evidence.values():
            for cid, evidence in entries.items():
                if (evidence.get("contribution", 0) or 0) > 0:
                    positive_ids.add(cid)
        for cid in positive_ids:
            c = s.lib.contents.get(cid) if hasattr(s, "lib") else None
            if not c:
                continue
            if c["domain"] == key or key in c["sub_tags"]:
                n += 1
        return n

    out = []
    for tpl in ad.get("reason_template", []):
        if not isinstance(tpl, str):
            continue
        # 种子通常会先经过 qa_check，但运行时也要自守：未知占位符不能原样
        # 泄漏到结果页，否则「可追溯」会退化成一条看不懂的模板字符串。
        tokens = re.findall(r"\{([^{}]+)\}", tpl)
        # 没有任何证据占位符的整句是写死的断言，无法在结果页下钻到
        # 具体行为；即使它来自旧种子，也宁可不展示。
        if not tokens:
            continue
        known = (set(("top_content", "dwell")) |
                 {"trait:" + t for t in TRAIT_AXIS} |
                 {"cross:" + a for a in AXES} |
                 {"n_" + d for d in DOMAIN_CN} |
                 {"n_" + t for t in SUBTAG_CN})
        if any(token not in known for token in tokens):
            continue
        # 未配对的大括号也视为坏模板；不要把半截占位符展示给观众。
        stripped = re.sub(r"\{[^{}]+\}", "", tpl)
        if "{" in stripped or "}" in stripped:
            continue
        # ---- 行门控
        needs_trait = re.findall(r"\{trait:([a-z_]+)\}", tpl)
        if any(p not in active_traits for p in needs_trait):
            continue
        needs_cross = re.findall(r"\{cross:([a-z_]+)\}", tpl)
        if any(a not in cross_axes for a in needs_cross):
            continue
        text = re.sub(r"\{trait:[a-z_]+\}|\{cross:[a-z_]+\}", "", tpl)

        # ---- 计数占位（n=0 → 丢弃）
        ok = True
        for key in re.findall(r"\{n_([a-z_]+)\}", text):
            n = count_for(key)
            if n <= 0:
                ok = False
                break
            text = text.replace("{n_%s}" % key, str(n))
        if not ok:
            continue

        # ---- 停留证据（无真实停留 → 丢弃；{dwell}/{top_content} 取同一证据行）
        if "{dwell}" in text or "{top_content}" in text:
            if dwell_top is None or dwell_sec <= 0:
                continue
            text = text.replace("{top_content}", dwell_top["title"])
            text = text.replace("{dwell}", str(dwell_sec))

        text = text.strip()
        if text:
            out.append(text)
    return out


# ================================================================ 会话管理

class SessionStore:
    """内存会话容器 + 可选的匿名聚合计数留存。

    ``metrics_path`` 只指向 :class:`AggregateMetricsStore` 的数字文件；不传
    （离线仿真、开发调用）就完全不落盘。无论是否启用留存，单个会话仍只在
    内存中存在并按 ``SESSION_TTL`` 清理。
    """

    def __init__(self, lib, metrics_path=None):
        self.lib = lib
        self.sessions = {}
        # 创建请求键只在会话存活期间保存在内存，用来把响应丢失后的重试
        # 合并回原会话；不会写入长期指标文件，也不会进入任何 API 响应。
        self.create_request_cache = {}
        # ThreadingHTTPServer 的请求线程会同时创建、读取、回收会话。
        # 会话自身的画像由 Session.lock 保护；这里再保护容器和聚合桶，避免
        # gc/delete 与 active()/create() 交错时迭代一个正在变化的 dict。
        self.lock = threading.RLock()
        self._probe_cursor = 0
        self.metrics_store = AggregateMetricsStore(metrics_path)
        loaded = self.metrics_store.snapshot()

        # 长期桶只恢复累计体验会话和最终反馈；其它行为与画像仍只在内存。
        self.feedback_counts = defaultdict(int, {
            "accurate": loaded.get("feedback_accurate", 0),
            "inaccurate": loaded.get("feedback_inaccurate", 0),
        })
        self.total_started = loaded.get("participants_total", 0)
        self.metrics_write_ok = bool(self.metrics_store.last_write_ok)

    # ---------------------------------------------------------- 聚合计数

    def _metric_snapshot_unlocked(self):
        """在 ``self.lock`` 已持有时生成严格白名单的数字快照。"""
        return {
            "participants_total": max(0, int(self.total_started)),
            "feedback_accurate": max(0, int(self.feedback_counts.get("accurate", 0))),
            "feedback_inaccurate": max(0, int(self.feedback_counts.get("inaccurate", 0))),
        }

    def _persist_metrics_unlocked(self):
        # 写失败不影响本次体验；内存桶仍是当前进程的真实状态，下一次
        # 聚合变更会再次尝试原子替换文件。
        self.metrics_write_ok = self.metrics_store.replace(self._metric_snapshot_unlocked())
        return self.metrics_write_ok

    @staticmethod
    def _ratio(numerator, denominator):
        return round(numerator / denominator, 3) if denominator else None

    def aggregate_stats(self):
        """返回后台/大屏可用的长期聚合指标，不含任何个体字段。"""
        with self.lock:
            raw = self._metric_snapshot_unlocked()
            write_ok = bool(self.metrics_write_ok)
            enabled = self.metrics_store.path is not None
        feedback_total = raw["feedback_accurate"] + raw["feedback_inaccurate"]
        return {
            "participants_total": raw["participants_total"],
            "feedback_total": feedback_total,
            "feedback_accurate": raw["feedback_accurate"],
            "feedback_inaccurate": raw["feedback_inaccurate"],
            # 这里的“准确率”是观众对结果点“准”的匿名认可率，不宣称
            # 行为画像存在客观真值；无反馈时用 null 而不是误导性的 0%。
            "accuracy_rate": self._ratio(raw["feedback_accurate"], feedback_total),
            "persistence_enabled": enabled,
            "persistence_healthy": write_ok if enabled else True,
        }

    def _feedback_transition(self, s, old, new):
        """接收 ``profile_feedback`` 事件造成的反馈变化。

        事件接口和专用 ``/api/feedback`` 接口最终共用同一个计数口径；
        只传递两个固定枚举值，不把会话内容交给持久化层。
        """
        if s is None or new not in ("accurate", "inaccurate"):
            return
        with self.lock:
            if old in ("accurate", "inaccurate"):
                self.feedback_counts[old] = max(0, self.feedback_counts[old] - 1)
            self.feedback_counts[new] += 1
            self._persist_metrics_unlocked()

    def remember_feedback(self, s, value):
        """按每个会话的最终反馈计数；改选时调整旧桶，避免重复累计。"""
        if value not in ("accurate", "inaccurate") or s is None:
            return False
        # 调用方通常已经持有 s.lock；RLock 也允许独立调用方安全使用此方法。
        with s.lock:
            if s.deleted or not s.finished:
                return False
            old = s.feedback
            if old == value:
                return True
            with self.lock:
                if old in ("accurate", "inaccurate"):
                    self.feedback_counts[old] = max(0, self.feedback_counts[old] - 1)
                s.feedback = value
                self.feedback_counts[value] += 1
                self._persist_metrics_unlocked()
        return True

    # ---------------------------------------------------------- 会话生命周期

    def create(self, probe_group=None, show_on_screen=True, request_id=None):
        # 回收过程需要按 Session.lock → SessionStore.lock 的顺序标记删除，
        # 因此不能在已经持有容器锁时顺带执行。
        self.gc()
        request_id = request_id.strip() if isinstance(request_id, str) else None
        with self.lock:
            if request_id:
                existing_sid = self.create_request_cache.get(request_id)
                existing = self.sessions.get(existing_sid)
                if existing is not None:
                    return existing
                self.create_request_cache.pop(request_id, None)
            # 未指定时按组轮换首屏探针；现场连续场次不会永远拿到同一
            # 组八张卡。显式传入合法组名仍可供后台/离线仿真复现实验。
            if not isinstance(probe_group, str) or probe_group not in PROBE_GROUPS:
                groups = list(PROBE_GROUPS) or ["default"]
                probe_group = groups[self._probe_cursor % len(groups)]
                self._probe_cursor += 1
            s = Session(self.lib, probe_group)
            s.show_on_screen = _safe_bool(show_on_screen, True)
            s._feedback_hook = self._feedback_transition
            s._create_request_id = request_id
            self.sessions[s.sid] = s
            if request_id:
                self.create_request_cache[request_id] = s.sid
            # 参与人数在创建时累计，和是否展示到大屏完全无关。
            self.total_started += 1
            self._persist_metrics_unlocked()
        return s

    def get(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
        if s and time.time() - s.created_at > SESSION_TTL:
            self._delete_session(sid, expected=s)
            return None
        return s

    def delete(self, sid):
        return self._delete_session(sid)

    def _delete_session(self, sid, expected=None):
        """原子停止一个会话，并从容器移除。

        事件和反馈路径本来就按 ``Session.lock → SessionStore.lock`` 取锁；
        删除沿用同一顺序。删除返回后，先前取得旧引用的请求也只能看到
        ``deleted=True``，不会在清除后继续生成结果或修改反馈。
        """
        if expected is None:
            with self.lock:
                s = self.sessions.get(sid)
        else:
            s = expected
        if s is None:
            return False
        with s.lock:
            with self.lock:
                if self.sessions.get(sid) is not s:
                    return False
                s.deleted = True
                self.sessions.pop(sid, None)
                request_id = getattr(s, "_create_request_id", None)
                if request_id and self.create_request_cache.get(request_id) == sid:
                    self.create_request_cache.pop(request_id, None)
                s._create_request_id = None
        return True

    def gc(self):
        with self.lock:
            now = time.time()
            expired = [(sid, s) for sid, s in self.sessions.items()
                       if now - s.created_at > SESSION_TTL]
        for sid, s in expired:
            self._delete_session(sid, expected=s)

    def active(self, limit=8):
        self.gc()
        with self.lock:
            active = sorted(self.sessions.values(), key=lambda s: -s.updated_at)[:limit]
        return active

    def reset(self):
        """只清当前内存会话；长期聚合数字故意保留。"""
        with self.lock:
            sessions = list(self.sessions.items())
        removed = 0
        for sid, s in sessions:
            removed += int(self._delete_session(sid, expected=s))
        return removed
