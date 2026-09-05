"""
参数离线校准 —— 《开发方案.md》§8.9 蒙特卡洛仿真

    uv run demo/simulate.py                 # 默认 300 次
    uv run demo/simulate.py -n 1000 --sweep # 扫 κ

关键约束：`build_screen` 必须调用线上同一份投放代码，否则校准出来的参数
在真实反馈回路下不成立 —— 这里直接用 engine.Feeder，不另写一份。

校准目标（§8.9）：
    top1 域分数中位数        0.85 ± 0.05     调 κ
    6 屏内常规观众收敛率      ≥ 85%           调 converge_threshold / 探索位
    特质轴脱离中性区平均条数  ≥ 2.0           调成对投放覆盖率
    画像判对率               ≥ 80%           综合
"""

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # Windows 控制台默认 GBK，强制 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import engine
from engine import POLE_NAME, TRAIT_AXIS, Feeder, Library, SessionStore

SEED_DIR = Path(__file__).resolve().parent.parent / "事件库"

# (主兴趣域, 副兴趣域, 特质倾向, 点击率, 平均停留秒, 类型)
PERSONAS = [
    ("tech_digital", "game_acg", {"price": 0.8, "depth": 0.7}, 0.35, 9.0, "regular"),
    ("life_shopping", "study_exam", {"price": 0.75}, 0.30, 6.0, "regular"),
    ("game_acg", "av_ent", {"novelty": 0.8}, 0.45, 4.0, "regular"),
    (
        "study_exam",
        "tech_digital",
        {"depth": 0.8, "expertise": 0.75},
        0.30,
        12.0,
        "regular",
    ),
    ("culture_art", "av_ent", {"depth": 0.85}, 0.28, 14.0, "regular"),
    ("sport_outdoor", "life_shopping", {"price": 0.3}, 0.33, 5.0, "regular"),
    (
        "social_hot",
        "life_shopping",
        {"price": 0.8, "decision": 0.3},
        0.34,
        7.0,
        "regular",
    ),
    ("av_ent", "game_acg", {"depth": 0.2, "expertise": 0.25}, 0.42, 3.0, "regular"),
    # 三类边缘用户
    ("__random__", None, {}, 0.40, 5.0, "edge"),
    ("tech_digital", None, {"depth": 0.9}, 0.08, 20.0, "edge"),
    (None, None, {}, 0.00, 0.0, "edge"),
]


def is_regular(p):
    return p[5] == "regular"


def click_prob(p, c):
    """兴趣匹配度越高越可能点；特质相符的内容也更容易被点开"""
    main, sub, traits, rate, _ = p[:5]
    if main is None:
        return 0.0
    if main == "__random__":
        return rate
    base = rate * (
        2.2 if c["domain"] == main else (1.2 if c["domain"] == sub else 0.35)
    )
    for t, w in c["traits"].items():
        axis, pole = TRAIT_AXIS[t]
        if axis in traits:
            want = traits[axis]  # 0~1，>0.5 偏正极
            aligned = want if pole == "pro" else 1 - want
            base *= (0.55 + 1.1 * aligned) ** w
    return min(0.95, base)


def dwell_time(p, c):
    _, _, traits, _, avg = p[:5]
    d = random.gauss(avg, avg * 0.4)
    if c.get("body_len") == "L":
        d *= 1.5
    return max(0.4, d)


def simulate(lib, feeder, persona, n_screens=6, min_screens=3):
    store = SessionStore(lib)
    s = store.create()
    seq = 0

    def emit(ev):
        nonlocal seq
        seq += 1
        ev["seq"] = seq
        ev["ts"] = time.time() * 1000
        s.on_event(ev)

    for i in range(n_screens):
        screen = feeder.build_screen(s)  # ← 线上同一份投放代码
        if not screen:
            break
        emit({"type": "screen_view", "props": {"screen_index": s.screen_index}})
        s.screen_index += 1
        for pos, c in enumerate(screen):
            cid = c["content_id"]
            props = {"position": pos, "screen_index": s.screen_index - 1}
            emit({"type": "content_impression", "content_id": cid, "props": props})
            if random.random() < click_prob(persona, c):
                emit({"type": "content_click", "content_id": cid, "props": props})
                dw = dwell_time(persona, c)
                emit(
                    {
                        "type": "content_dwell",
                        "content_id": cid,
                        "props": dict(
                            props,
                            dwell_ms=int(dw * 1000),
                            scroll_depth=round(random.uniform(0.5, 1.0), 2),
                        ),
                    }
                )
                if random.random() < 0.12:
                    emit({"type": "content_review", "content_id": cid, "props": props})
            elif random.random() < 0.4:
                emit({"type": "content_skip", "content_id": cid, "props": props})
        if i + 1 >= min_screens and s.converged():
            break

    # 离线仿真保留旧版的消歧补足，以便与历史校准结果保持可比；
    # 线上手机端结束流程不会调用这段逻辑，也不会向观众显示提问。
    for _ in range(2):
        if s.converged():
            break
        card = feeder.next_card(s)
        if not card:
            break
        axis = card["axis"]
        want = persona[2].get(axis, 0.5)
        pole = "pro" if random.random() < want else "con"
        emit(
            {
                "type": "probe_choice",
                "props": {"axis": axis, "picked": pole, "label": card["prompt"]},
            }
        )
    return s


def judge(s, persona):
    """画像判对率：虚拟用户的真实 persona 已知，可直接比对（仿真独有指标）

    注意这是宽松口径：意图特质"未展示"不扣分、无关轴的误报也不扣分，
    只检查"没有说反话"。展示结论的真实精度另见 trait_precision。
    """
    if not is_regular(persona):
        return None
    main, _, traits, _, _ = persona[:5]
    if main in (None, "__random__"):
        return None
    top, _, _ = s.top_domain()
    if top != main:
        return False
    got = s.trait_scores()
    for axis, want in traits.items():
        if abs(want - 0.5) < 0.2:
            continue
        expect = POLE_NAME[axis][0] if want > 0.5 else POLE_NAME[axis][1]
        if got[axis]["pole"] and got[axis]["pole"] != expect:
            return False
    return True


def trait_precision(s, persona):
    """展示特质的真实精度（严格口径）：判对 /（判对+判错极+误报）

    与 judge() 的区别：意图特质必须真的被展示才算对；用户没有意图的轴
    被展示即计一次误报。这是"精准猜测"的真实口径。
    返回 None 表示该 persona 没有可评判的意图特质。
    """
    if not is_regular(persona):
        return None
    _, _, traits, _, _ = persona[:5]
    intended = {
        axis: POLE_NAME[axis][0] if want > 0.5 else POLE_NAME[axis][1]
        for axis, want in traits.items()
        if abs(want - 0.5) >= 0.2
    }
    if not intended:
        return None
    got = s.trait_scores()
    shown = {axis: t["pole"] for axis, t in got.items() if t["pole"]}
    if not shown:
        return 0.0
    ok = sum(1 for a, pl in shown.items() if intended.get(a) == pl)
    return ok / len(shown)


def run(n, lib):
    feeder = Feeder(lib)
    tops, conv, offaxis, right, total_judged = [], 0, [], 0, 0
    rtops, rconv, roffaxis, rcount = [], 0, [], 0
    precisions = []
    personas_seen = {}
    for i in range(n):
        p = PERSONAS[i % len(PERSONAS)]
        s = simulate(lib, feeder, p)
        _, score, _ = s.top_domain()
        tops.append(score)
        conv += 1 if s.converged() else 0
        offaxis.append(sum(1 for t in s.trait_scores().values() if t["pole"]))
        if is_regular(p):
            rcount += 1
            rtops.append(score)
            rconv += 1 if s.converged() else 0
            roffaxis.append(sum(1 for t in s.trait_scores().values() if t["pole"]))
            tp = trait_precision(s, p)
            if tp is not None:
                precisions.append(tp)
        v = judge(s, p)
        if v is not None:
            total_judged += 1
            right += 1 if v else 0
        personas_seen[s.persona()] = personas_seen.get(s.persona(), 0) + 1
    return {
        "median_top": statistics.median(tops),
        "mean_top": statistics.mean(tops),
        "converge": conv / n,
        "off_axis": statistics.mean(offaxis),
        "regular_median_top": statistics.median(rtops),
        "regular_converge": rconv / rcount,
        "regular_off_axis": statistics.mean(roffaxis),
        "accuracy": (right / total_judged) if total_judged else 0.0,
        "trait_precision": statistics.mean(precisions) if precisions else 0.0,
        "personas": personas_seen,
    }


def line(r, kappa):
    def mark(ok):
        return "✓" if ok else "✗"

    return (
        "κ=%.1f  常规 top1 中位数 %.3f %s  收敛率 %5.1f%% %s  "
        "脱离中性轴 %.2f %s  判对率 %5.1f%% %s  展示精度 %5.1f%%"
    ) % (
        kappa,
        r["regular_median_top"],
        mark(0.80 <= r["regular_median_top"] <= 0.90),
        r["regular_converge"] * 100,
        mark(r["regular_converge"] >= 0.85),
        r["regular_off_axis"],
        mark(r["regular_off_axis"] >= 2.0),
        r["accuracy"] * 100,
        mark(r["accuracy"] >= 0.80),
        r["trait_precision"] * 100,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=300)
    ap.add_argument("--sweep", action="store_true", help="扫 κ 找最优值")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed)

    lib = Library(SEED_DIR)
    print(
        "内容库：%d 条（探针 %d）· 广告 %d 条 · 成对探针 %d 组\n"
        % (
            len(lib.contents),
            len(lib.probes),
            len(lib.ads),
            sum(len(v) for v in lib.pairs.values()),
        )
    )

    if args.sweep:
        print(
            "校准目标：常规观众 top1 中位数 0.85±0.05 · 收敛率 ≥85% · 脱离中性轴 ≥2.0 · 判对率 ≥80%\n"
        )
        for k in [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]:
            engine.KAPPA = k
            random.seed(args.seed)
            print(line(run(args.n, lib), k))
        return

    r = run(args.n, lib)
    print(line(r, engine.KAPPA))
    print(
        "全量含边缘：top1 中位数 %.3f · 收敛率 %.1f%% · 脱离中性轴 %.2f"
        % (r["median_top"], r["converge"] * 100, r["off_axis"])
    )
    print("\n浏览标签分布（前 10）：")
    for name, c in sorted(r["personas"].items(), key=lambda kv: -kv[1])[:10]:
        print("  %-24s %d" % (name, c))


if __name__ == "__main__":
    main()
