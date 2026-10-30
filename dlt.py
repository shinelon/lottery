#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超级大乐透 历史数据分析 / 选号 / 回测工具（纯标准库，零第三方依赖）

用法:
    py dlt.py                # 抓取数据 -> 回测 -> 出下一期 5 注 + 报告
    py dlt.py --no-fetch     # 只用本地 data/dlt.csv
    py dlt.py --window 120   # 改变数据窗口期数

设计原则:
    1) 大乐透每期独立同分布，本工具不"预测"，只做统计筛选 + 组合设计 + 严格复盘。
    2) 参数刻意保持最少（1 组权重），避免在 100 期小样本上过拟合。
    3) 回测切分: 前 60 期定规则 / 后 40 期盲测；结论只允许"优于/不差异/劣于随机"。

规则口径（2026-01-31 第 26014 期起的新规则）:
    前区 35 选 5 + 后区 12 选 2，单注 2 元；共 7 个奖级，13 个中奖条件。
    三等 5000 / 四等 300 / 五等 150 / 六等 15 / 七等 5 元；
    奖池 >= 8 亿元时固定奖上调（6666 / 380 / 200 / 18 / 7）。
    开奖时间: 每周一、三、六 21:25；当期销售截止 21:00。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from itertools import combinations

# ---------------------------------------------------------------- 配置

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_CSV = os.path.join(BASE, "data", "dlt.csv")
REPORT_DIR = os.path.join(BASE, "reports", "dlt")
HIST_CSV = os.path.join(REPORT_DIR, "history.csv")

WINDOW = 100          # 分析窗口: 近 100 期
RULE_N = 60           # 前 60 期定规则
TEST_N = 40           # 后 40 期盲测
FETCH_COUNT = 260     # 多抓一些做特征预热
FREQ_WINDOW = 50      # 前区频次统计窗口
OM_WINDOW = 100       # 遗漏参考窗口
BACK_FREQ_WINDOW = 30 # 后区频次窗口
TICKETS = 5           # 每期注数
PRICE = 2             # 单注价格
MC_TRIALS = 5000      # 蒙特卡洛次数
SEED = 20261030       # 固定随机种子，保证可复现

W_FREQ = 0.6          # 权重: 近 50 期频次
W_OMIT = 0.4          # 权重: 遗漏回归值

FRONT_NUMS = list(range(1, 36))   # 前区 01-35
BACK_NUMS = list(range(1, 13))    # 后区 01-12
FRONT_PICK = 5
BACK_PICK = 2

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

DRAW_WEEKDAYS = {0, 2, 5}   # 周一 / 周三 / 周六  (Mon=0)
SALE_CUTOFF_HOUR = 21       # 2023-01-01 起 21:00 停售，21:25 开奖

POOL_BOOST = 8.0e8          # 奖池 >= 8 亿元时固定奖上调

# 固定奖: 中奖条件 -> (奖池 < 8 亿, 奖池 >= 8 亿)
FIXED_PRIZE = {
    (5, 0): (5000, 6666), (4, 2): (5000, 6666),
    (4, 1): (300, 380),
    (4, 0): (150, 200), (3, 2): (150, 200),
    (3, 1): (15, 18), (2, 2): (15, 18),
    (3, 0): (5, 7), (2, 1): (5, 7), (1, 2): (5, 7), (0, 2): (5, 7),
}
FLOATING_PRIZE = {(5, 2): "一等奖", (5, 1): "二等奖"}

# 奖级 -> 中奖条件（2026-01-31 起）
TIERS = [
    ("一等奖", ((5, 2),)),
    ("二等奖", ((5, 1),)),
    ("三等奖", ((5, 0), (4, 2))),
    ("四等奖", ((4, 1),)),
    ("五等奖", ((4, 0), (3, 2))),
    ("六等奖", ((3, 1), (2, 2))),
    ("七等奖", ((3, 0), (2, 1), (1, 2), (0, 2))),
]


def fit_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 抓取

def http_get(url: str, referer: str | None = None, timeout: int = 25) -> str:
    """优先用系统 curl（本机 urllib 的 TLS 请求会被拦截），urllib 兜底。"""
    raw: bytes | None = None

    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        cmd = [curl, "-sS", "--max-time", str(timeout), "-A", UA]
        if referer:
            cmd += ["-e", referer]
        cmd.append(url)
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            if res.returncode == 0 and res.stdout:
                raw = res.stdout
            else:
                err = res.stderr.decode("utf-8", "replace").strip()[:200]
                raise RuntimeError(f"curl 退出码 {res.returncode}: {err}")
        except FileNotFoundError:
            pass

    if raw is None:
        headers = {"User-Agent": UA, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()

    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _num(text) -> str:
    return str(text or "").replace(",", "").strip()


def fetch_official(count: int) -> list[dict]:
    """中国体彩网官方接口（gameNo=85），JSON。每页上限 100 期。"""
    per_page = 100
    pages = max(1, math.ceil(count / per_page))
    draws: list[dict] = []

    for page in range(1, pages + 1):
        url = ("https://webapi.sporttery.cn/gateway/lottery/"
               f"getHistoryPageListV1.qry?gameNo=85&provinceId=0"
               f"&pageSize={per_page}&isVerify=1&pageNo={page}")
        payload = json.loads(http_get(url, referer="https://www.lottery.gov.cn/"))
        if not payload.get("success"):
            raise RuntimeError(f"官方接口返回 {payload.get('errorMessage')}")
        rows = (payload.get("value") or {}).get("list") or []
        if not rows:
            break

        for row in rows:
            nums = [int(x) for x in str(row.get("lotteryDrawResult", "")).split()]
            if len(nums) != FRONT_PICK + BACK_PICK:
                continue
            draws.append({
                "code": str(row.get("lotteryDrawNum", "")).strip(),
                "date": str(row.get("lotteryDrawTime", "")).strip()[:10],
                "front": sorted(nums[:FRONT_PICK]),
                "back": sorted(nums[FRONT_PICK:]),
                "sales": _num(row.get("totalSaleAmount")),
                "pool": _num(row.get("poolBalanceAfterdraw")),
            })
        if len(draws) >= count:
            break
    return draws[:count]


def _cells(row: str) -> list[str]:
    body = re.sub(r"<!--.*?-->", "", row, flags=re.S)
    return [re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)]


def fetch_500(count: int) -> list[dict]:
    """500彩票网备用源，HTML 表格。"""
    url = (f"https://datachart.500.com/dlt/history/newinc/history.php"
           f"?limit={count}&sort=0")
    html = http_get(url, referer="https://datachart.500.com/dlt/history/history.shtml")

    draws = []
    for row in re.findall(r'<tr class="t_tr1">(.*?)</tr>', html, re.S):
        cells = _cells(row)
        if len(cells) < 15:
            continue
        code = cells[0]
        if len(code) != 5 or not code.isdigit():
            continue
        fronts = [int(x) for x in cells[1:6]]
        backs = [int(x) for x in cells[6:8]]
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", cells[-1])
        draws.append({
            "code": code,
            "date": dates[0] if dates else "",
            "front": sorted(fronts),
            "back": sorted(backs),
            "sales": _num(cells[13]),
            "pool": _num(cells[8]),
        })
    return draws


def validate(draws: list[dict]) -> list[dict]:
    """排序 / 去重 / 范围校验 / 期号连续性检查。期号为 5 位（年份 2 位 + 序号 3 位）。"""
    seen, good = set(), []
    for d in draws:
        code = str(d.get("code", ""))
        front, back = d.get("front") or [], d.get("back") or []
        if len(code) != 5 or not code.isdigit():
            log(f"  [丢弃] 期号异常: {code!r}")
            continue
        if len(set(front)) != FRONT_PICK or not all(1 <= x <= 35 for x in front):
            log(f"  [丢弃] {code} 前区非法: {front}")
            continue
        if len(set(back)) != BACK_PICK or not all(1 <= x <= 12 for x in back):
            log(f"  [丢弃] {code} 后区非法: {back}")
            continue
        if code in seen:
            continue
        seen.add(code)
        good.append({**d, "front": sorted(front), "back": sorted(back)})

    good.sort(key=lambda d: d["code"])

    gaps = 0
    for a, b in zip(good, good[1:]):
        if a["code"][:2] == b["code"][:2]:
            if int(b["code"][2:]) != int(a["code"][2:]) + 1:
                gaps += 1
    log(f"  校验: {len(good)} 期, 期号缺口 {gaps} 处")
    return good


def load_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    draws = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            draws.append({
                "code": row["期号"],
                "date": row["日期"],
                "front": [int(row[f"前{i}"]) for i in range(1, FRONT_PICK + 1)],
                "back": [int(row["后1"]), int(row["后2"])],
                "sales": row.get("销量", ""),
                "pool": row.get("奖池", ""),
            })
    return draws


def save_csv(path: str, draws: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["期号", "日期", "前1", "前2", "前3", "前4", "前5",
                    "后1", "后2", "销量", "奖池"])
        for d in draws:
            w.writerow([d["code"], d["date"], *d["front"], *d["back"],
                        d["sales"], d["pool"]])


def get_draws(use_network: bool) -> list[dict]:
    if use_network:
        for name, fn in (("中国体彩网", fetch_official), ("500彩票网", fetch_500)):
            try:
                log(f"[1/6] 抓取数据 <- {name}")
                draws = validate(fn(FETCH_COUNT))
                if len(draws) >= 120:
                    save_csv(DATA_CSV, draws)
                    log(f"  已写入 {DATA_CSV}")
                    return draws
                log(f"  {name} 仅返回 {len(draws)} 期，不足，尝试下一个源")
            except Exception as exc:
                log(f"  {name} 失败: {exc}")

    draws = validate(load_csv(DATA_CSV))
    if not draws:
        raise SystemExit("没有可用数据：联网抓取失败且本地缓存不存在。")
    log(f"[1/6] 使用本地缓存 {DATA_CSV} ({len(draws)} 期)")
    return draws


# ---------------------------------------------------------------- 特征与打分

def pct_rank(values: dict[int, float]) -> dict[int, float]:
    """把 {号码: 数值} 映射到 [0,1] 的分位。"""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    if n <= 1:
        return {k: 0.5 for k in values}
    return {k: i / (n - 1) for i, (k, _) in enumerate(items)}


def omission(past: list[dict], n: int, key: str = "front") -> int:
    om = 0
    for d in reversed(past):
        if n in d[key]:
            break
        om += 1
    return om


def back_score_map(past: list[dict], w: float = 0.5,
                   fw: int = BACK_FREQ_WINDOW) -> tuple[dict[int, float], dict[int, int]]:
    """后区打分。生产逻辑与敏感性检验共用这一个函数，避免两套实现漂移。"""
    f = Counter(n for d in past[-fw:] for n in d["back"])
    om = {n: omission(past, n, "back") for n in BACK_NUMS}
    cap = max(max(om.values()), 1)
    fp = pct_rank({n: f.get(n, 0) for n in BACK_NUMS})
    mid = {n: 1.0 - abs(om[n] / cap - 0.5) * 2 for n in BACK_NUMS}
    return {n: w * fp[n] + (1 - w) * mid[n] for n in BACK_NUMS}, om


def build_features(past: list[dict]) -> dict:
    """只用 past（严格早于当期）构建特征。"""
    recent = past[-FREQ_WINDOW:]

    ff = Counter(n for d in recent for n in d["front"])
    fom = {n: omission(past, n, "front") for n in FRONT_NUMS}
    cap = max(max(fom.values()), 1)
    ff_pct = pct_rank({n: ff.get(n, 0) for n in FRONT_NUMS})
    fom_mid = {n: 1.0 - abs(fom[n] / cap - 0.5) * 2 for n in FRONT_NUMS}
    front_score = {n: W_FREQ * ff_pct[n] + W_OMIT * fom_mid[n] for n in FRONT_NUMS}

    back_score, bom = back_score_map(past)

    return {
        "front_score": front_score,
        "back_score": back_score,
        "front_freq": ff,
        "front_om": fom,
        "back_om": bom,
    }


# ---------------------------------------------------------------- 形态

def zone_pattern(front: list[int]) -> str:
    z = [0, 0, 0]
    for x in front:
        z[0 if x <= 12 else (1 if x <= 24 else 2)] += 1
    return ":".join(str(v) for v in z)


def form_ok(front: list[int], last_front: list[int]) -> bool:
    total = sum(front)
    if not 70 <= total <= 110:
        return False
    odd = sum(1 for x in front if x % 2)
    if not 1 <= odd <= 4:
        return False
    if "0" in zone_pattern(front).split(":"):
        return False
    if len(set(front) & set(last_front)) > 2:
        return False
    for i in range(len(front) - 2):
        if front[i] + 1 == front[i + 1] and front[i + 1] + 1 == front[i + 2]:
            return False
    return True


def form_signature(front: list[int]) -> tuple:
    odd = sum(1 for x in front if x % 2)
    return (odd, zone_pattern(front))


def back_pairs(back_score: dict[int, float]) -> list[tuple[int, int]]:
    """取最高分 3 个后区号，组合成 2/2/1 的 5 对，保证注间结构一致且分散。"""
    top = sorted(BACK_NUMS, key=lambda n: back_score[n], reverse=True)[:3]
    a, b, c = sorted(top)
    pairs = [(a, b), (a, b), (a, c), (a, c), (b, c)]
    return [tuple(sorted(p)) for p in pairs]


def select_tickets(feat: dict, last_front: list[int]) -> list[dict]:
    """从高分号池中枚举候选组合，按分数选 5 注，保证注间重合 <=2 且形态分散。"""
    score = feat["front_score"]
    order = sorted(FRONT_NUMS, key=lambda n: score[n], reverse=True)

    candidates: list[tuple[float, tuple]] = []
    for topn in (14, 16, 18, 20, 22, 25, 28, 35):
        pool = sorted(order[:topn])
        if len(pool) < FRONT_PICK:
            continue
        candidates = []
        for combo in combinations(pool, FRONT_PICK):
            if form_ok(list(combo), last_front):
                candidates.append((sum(score[n] for n in combo), combo))
        if len(candidates) >= 300:
            break

    if not candidates:
        raise RuntimeError("形态过滤后无候选组合，请放宽条件")

    candidates.sort(key=lambda x: x[0], reverse=True)

    chosen: list[tuple[float, tuple]] = []
    sig_count: Counter = Counter()
    for sc, combo in candidates:
        if any(len(set(combo) & set(c[1])) > 2 for c in chosen):
            continue
        sig = form_signature(list(combo))
        if sig_count[sig] >= 1:
            continue
        chosen.append((sc, combo))
        sig_count[sig] += 1
        if len(chosen) == TICKETS:
            break

    # 形态不够分散时放宽（仍保持重合 <=2）
    if len(chosen) < TICKETS:
        for sc, combo in candidates:
            if any(len(set(combo) & set(c[1])) > 2 for c in chosen):
                continue
            if any(combo == c[1] for c in chosen):
                continue
            chosen.append((sc, combo))
            if len(chosen) == TICKETS:
                break

    if len(chosen) < TICKETS:
        raise RuntimeError(f"只能生成 {len(chosen)} 注，无法满足组合约束")

    pairs = back_pairs(feat["back_score"])
    tickets = []
    for i, (sc, combo) in enumerate(chosen):
        a, b = pairs[i]
        tickets.append({
            "front": list(combo),
            "back": [a, b],
            "score": sc,
            "back_score": 0.5 * (feat["back_score"][a] + feat["back_score"][b]),
        })
    return tickets


# ---------------------------------------------------------------- 结算

def prize_table(pool_yuan: float) -> dict:
    """按奖池水位给出 (中奖条件 -> (奖级, 单注奖金))，浮动奖金额记 0。"""
    boosted = pool_yuan >= POOL_BOOST
    table = {}
    for cond, (base, boost) in FIXED_PRIZE.items():
        name = next(n for n, conds in TIERS if cond in conds)
        table[cond] = (name, float(boost if boosted else base))
    for cond, name in FLOATING_PRIZE.items():
        table[cond] = (name, 0.0)
    return table


def latest_pool(draws: list[dict]) -> float:
    try:
        return float(draws[-1].get("pool") or 0)
    except (TypeError, ValueError):
        return 0.0


def settle(front: list[int], back: list[int], draw: dict,
           table: dict) -> tuple[int, int, str | None, float]:
    nf = len(set(front) & set(draw["front"]))
    nb = len(set(back) & set(draw["back"]))
    name, money = table.get((nf, nb), (None, 0.0))
    return nf, nb, name, float(money or 0.0)


# ---------------------------------------------------------------- 回测

def backtest(draws: list[dict], start: int, end: int, table: dict) -> list[dict]:
    rows = []
    for idx in range(start, end):
        feat = build_features(draws[:idx])
        tickets = select_tickets(feat, draws[idx - 1]["front"])
        for k, t in enumerate(tickets, 1):
            nf, nb, name, money = settle(t["front"], t["back"], draws[idx], table)
            rows.append({
                "code": draws[idx]["code"],
                "date": draws[idx]["date"],
                "idx": k,
                "front": t["front"],
                "back": t["back"],
                "hit_front": nf,
                "hit_back": nb,
                "prize": name,
                "money": money,
            })
    return rows


def monte_carlo(draws: list[dict], start: int, end: int, trials: int) -> dict:
    """随机选号的对照分布。

    三种零分布，方差不同，必须分开比：
      front_hits : 200 注前区各自随机，统计前区命中总数（期望 142.9）
      back_ind   : 200 注后区各自随机（普通玩家的买法），统计后区命中总数
      back_match : 每期随机 3 个后区号、按 2/2/1 组 5 对（与本方案结构一致），
                   注间相关导致方差更大，判定后区只认这一条
    """
    rng = random.Random(SEED ^ 0x0D17)
    front_hits, back_ind, back_match = [], [], []
    front3, back2 = [], []
    for _ in range(trials):
        fh = bi = bm = f3 = b2 = 0
        for idx in range(start, end):
            draw = draws[idx]
            for _ in range(TICKETS):
                f = rng.sample(FRONT_NUMS, FRONT_PICK)
                b = rng.sample(BACK_NUMS, BACK_PICK)
                nf = len(set(f) & set(draw["front"]))
                nb = len(set(b) & set(draw["back"]))
                fh += nf
                bi += nb
                if nf >= 3:
                    f3 += 1
            b3 = rng.sample(BACK_NUMS, 3)
            for p in ((b3[0], b3[1]), (b3[0], b3[1]), (b3[0], b3[2]),
                      (b3[0], b3[2]), (b3[1], b3[2])):
                hit = len(set(p) & set(draw["back"]))
                bm += hit
                if hit == 2:
                    b2 += 1
        front_hits.append(fh)
        back_ind.append(bi)
        back_match.append(bm)
        front3.append(f3)
        back2.append(b2)
    return {"n": (end - start) * TICKETS, "front_hits": front_hits,
            "back_ind": back_ind, "back_match": back_match,
            "front3": front3, "back2": back2}


def rolling_check(draws: list[dict], test_n: int, table: dict,
                  step: int = 20) -> list[dict]:
    """把同一套规则在多个滚动窗口上重跑，检验优势是否只在某一段偶然出现。"""
    out = []
    for st in range(RULE_N, len(draws) - test_n + 1, step):
        rows = backtest(draws, st, st + test_n, table)
        out.append({
            "start": draws[st]["code"],
            "end": draws[st + test_n - 1]["code"],
            "back": sum(r["hit_back"] for r in rows),
            "front": sum(r["hit_front"] for r in rows),
            "money": sum(r["money"] for r in rows),
            "notes": len(rows),
        })
    return out


def back_sensitivity(draws: list[dict], start: int, end: int,
                     weights=(0.3, 0.4, 0.5, 0.6, 0.7),
                     windows=(20, 30, 50, 100)) -> list[dict]:
    """后区打分参数的敏感性: 只改权重/频次窗口, 看命中总数摆幅。"""
    out = []
    for w in weights:
        for fw in windows:
            hits = 0
            for idx in range(start, end):
                sc, _ = back_score_map(draws[:idx], w, fw)
                pairs = back_pairs(sc)
                for a, b in pairs:
                    hits += len({a, b} & set(draws[idx]["back"]))
            out.append({"w": w, "fw": fw, "hits": hits})
    return out


def percentile(values: list[float], x: float) -> float:
    p = 100.0 * sum(1 for v in values if v <= x) / len(values)
    return min(p, 99.9)


def verdict(actual: int, dist: list[float]) -> str:
    p = percentile(dist, actual)
    if p >= 99.9:
        return "超出零分布上界（p<0.001）"
    if p >= 95:
        return f"显著优于随机（第 {p:.1f} 百分位）"
    if p <= 5:
        return f"显著劣于随机（第 {p:.1f} 百分位）"
    return f"与随机无差异（第 {p:.1f} 百分位）"


# ---------------------------------------------------------------- 下一期

def next_issue(latest: dict, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now()
    last_date = datetime.strptime(latest["date"], "%Y-%m-%d").date()

    target = last_date + timedelta(days=1)
    while target.weekday() not in DRAW_WEEKDAYS:
        target += timedelta(days=1)
    if target == now.date() and now.hour >= SALE_CUTOFF_HOUR:
        target += timedelta(days=1)
        while target.weekday() not in DRAW_WEEKDAYS:
            target += timedelta(days=1)

    last_year, last_seq = int(latest["code"][:2]), int(latest["code"][2:])
    if target.year % 100 == last_year:
        code = f"{last_year:02d}{last_seq + 1:03d}"
    else:
        code = f"{target.year % 100:02d}001"
    return code, target.isoformat()


# ---------------------------------------------------------------- 报告

def fmt_nums(nums: list[int]) -> str:
    return " ".join(f"{x:02d}" for x in nums)


def combo_count(nf: int, nb: int) -> int:
    """命中 nf 个前区 + nb 个后区的组合数（分母 C(35,5)*C(12,2)）。"""
    return (math.comb(FRONT_PICK, nf) * math.comb(35 - FRONT_PICK, FRONT_PICK - nf)
            * math.comb(BACK_PICK, nb) * math.comb(12 - BACK_PICK, BACK_PICK - nb))


def tier_counts() -> list[tuple[str, int, tuple]]:
    """每个奖级的中奖组合数与条件，全部由组合数现算，不手写概率。"""
    out = []
    for name, conds in TIERS:
        cnt = sum(combo_count(nf, nb) for nf, nb in conds)
        out.append((name, cnt, conds))
    return out


def write_report(path: str, code: str, target_date: str, tickets: list[dict],
                 feat: dict, last: dict, bt_rows: list[dict], mc: dict,
                 roll: list[dict], sens: list[dict],
                 pool: float, window_start: str, bt_start: int, bt_end: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    n_ticks = len(bt_rows)
    cost = n_ticks * PRICE
    win = [r for r in bt_rows if r["money"] > 0 or r["prize"] in FLOATING_PRIZE.values()]
    money = sum(r["money"] for r in bt_rows)
    back_hits = sum(r["hit_back"] for r in bt_rows)
    front_hits = sum(r["hit_front"] for r in bt_rows)
    front3 = sum(1 for r in bt_rows if r["hit_front"] >= 3)
    back2 = sum(1 for r in bt_rows if r["hit_back"] == BACK_PICK)

    m_mean = statistics.fmean(mc["back_match"])
    m_sd = statistics.pstdev(mc["back_match"])
    i_mean = statistics.fmean(mc["back_ind"])
    i_sd = statistics.pstdev(mc["back_ind"])
    f_mean = statistics.fmean(mc["front_hits"])
    f_sd = statistics.pstdev(mc["front_hits"])
    v_back = verdict(back_hits, mc["back_match"])
    v_front = verdict(front_hits, mc["front_hits"])
    p_back = percentile(mc["back_match"], back_hits)
    p_front = percentile(mc["front_hits"], front_hits)

    lines = []
    a = lines.append
    a(f"# 超级大乐透 {code} 期分析报告")
    a("")
    a(f"- 目标期: **{code}**（{target_date}）")
    a(f"- 投注方案: 单式 **{TICKETS} 注**，共 {TICKETS * PRICE} 元（不加追投）")
    a(f"- 数据窗口: 近 {WINDOW} 期（{window_start} – {last['code']}），"
      f"盲测段 {bt_rows[0]['code']} – {last['code']}")
    a(f"- 当期奖池: {pool:,.0f} 元（固定奖按 "
      f"{'8 亿以上' if pool >= POOL_BOOST else '8 亿以下'}档计）")
    a(f"- 生成时间: {datetime.now():%Y-%m-%d %H:%M}")
    a("")
    a("## 一、本期推荐注单")
    a("")
    a("| 注 | 前区 | 后区 | 和值 | 奇偶 | 三区 | 重号 | 组合分 |")
    a("|---|---|---|---|---|---|---|---|")
    for i, t in enumerate(tickets, 1):
        f = t["front"]
        odd = sum(1 for x in f if x % 2)
        rep = len(set(f) & set(last["front"]))
        a(f"| {i} | {fmt_nums(f)} | {' '.join(f'{b:02d}' for b in t['back'])} | "
          f"{sum(f)} | {odd}:{FRONT_PICK - odd} | {zone_pattern(f)} | {rep} | "
          f"{t['score']:.2f} |")
    a("")
    a("## 二、每注选号理由")
    a("")
    bs = feat["back_score"]
    for i, t in enumerate(tickets, 1):
        f = t["front"]
        odd = sum(1 for x in f if x % 2)
        top = sorted(f, key=lambda n: feat["front_score"][n], reverse=True)[:3]
        tag = ", ".join(
            f"{n:02d}(频{feat['front_freq'].get(n, 0)}次/遗漏{feat['front_om'][n]})"
            for n in top)
        a(f"**第 {i} 注** `{fmt_nums(f)} + {fmt_nums(t['back'])}`  "
          f"和值 {sum(f)}，奇偶 {odd}:{FRONT_PICK - odd}，三区 {zone_pattern(f)}，"
          f"与 {last['code']} 期重号 {len(set(f) & set(last['front']))} 个。"
          f"核心高分号: {tag}；后区 {' '.join(f'{b:02d}' for b in t['back'])} "
          f"得分 {t['back_score']:.2f}（遗漏 "
          f"{', '.join(str(feat['back_om'][b]) for b in t['back'])} 期）。")
        a("")

    a("## 三、5 注覆盖度")
    a("")
    all_nums = sorted({n for t in tickets for n in t["front"]})
    a(f"- 覆盖前区 {len(all_nums)} / 35 个: {fmt_nums(all_nums)}")
    backs = sorted({b for t in tickets for b in t["back"]})
    a(f"- 后区覆盖: {' '.join(f'{b:02d}' for b in backs)}"
      f"（取最高分 3 个，按 2/2/1 组成 5 对）")
    a(f"- 注间最大重合: "
      f"{max(len(set(x['front']) & set(y['front'])) for x, y in combinations(tickets, 2))} 个"
      f"（约束上限 2）")
    odd_dist = Counter(sum(1 for v in t["front"] if v % 2) for t in tickets)
    a("- 奇偶分布: " + "，".join(
        f"{k}:{FRONT_PICK - k} × {v} 注" for k, v in sorted(odd_dist.items())))
    zc = Counter(zone_pattern(t["front"]) for t in tickets)
    a("- 三区分布: " + "，".join(f"{k} × {v} 注" for k, v in sorted(zc.items())))
    a(f"- 和值区间: {min(sum(t['front']) for t in tickets)} – "
      f"{max(sum(t['front']) for t in tickets)}（约束 70–110）")
    a("")

    a("## 四、回测结论（盲测期）")
    a("")
    a(f"规则用前 {RULE_N} 期确定，参数冻结后在**后 {TEST_N} 期**盲测，"
      f"共 {n_ticks} 注、成本 {cost} 元。")
    a("")
    a("### 4.1 后区：两种零分布必须分开比")
    a("")
    a("本方案 5 注共只用 3 个后区号，按 2/2/1 组成 5 对。"
      "**单注后区命中期望仍是 2×(2/12)=1/3 个号，"
      "但注间共享号码会让后区命中总数的方差比“5 注各自随机”大。**"
      "所以不能只拿独立随机的窄分布当基准，否则会把方差误判成优势。")
    a("")
    a("| 零分布 | 期望 | 1σ | 本方案 | 百分位 | 判定 |")
    a("|---|---|---|---|---|---|")
    a(f"| 结构一致（每期随机 3 后区，2/2/1） | {m_mean:.1f} | ±{m_sd:.1f} | "
      f"{back_hits} | {p_back:.1f} | {v_back} |")
    a(f"| 200 注各自随机后区 | {i_mean:.1f} | ±{i_sd:.1f} | "
      f"{back_hits} | {percentile(mc['back_ind'], back_hits):.1f} | 参考 |")
    a("")
    a("### 4.2 前区")
    a("")
    a("| 指标 | 本方案 | 随机基准期望 | 随机 1σ | 百分位 | 判定 |")
    a("|---|---|---|---|---|---|")
    a(f"| 前区命中总数 | {front_hits} | {f_mean:.1f} | ±{f_sd:.1f} | "
      f"{p_front:.1f} | {v_front} |")
    a(f"| 单注前区 ≥3 个 | {front3} | {n_ticks * 4501 / 324632:.1f} | — | — | 参考 |")
    a(f"| 后区两个全中 | {back2} | {n_ticks / 66:.1f} | — | — | 参考 |")
    a("")
    a(f"- 中奖注数 {len(win)} / {n_ticks}（随机期望约 {n_ticks / 15:.1f}），"
      f"固定奖回款 {money:.0f} 元，投入 {cost} 元，"
      f"回报率 {100 * money / cost:.1f}%")
    pz = Counter(r["prize"] for r in bt_rows if r["prize"])
    if pz:
        a("- 奖级分布: " + "，".join(f"{k} × {v}" for k, v in pz.most_common()))
    a(f"- 蒙特卡洛: {MC_TRIALS} 次，每次模拟 40 期 × 5 注")
    a("")
    a("> 浮动奖（一、二等奖）无法在回测里折算成金额，上式只统计固定奖；"
      "返奖率约 51%，其中一、二等奖占大头，短样本必然碰不到。"
      "**判定标准看命中分布是否跳出随机区间，而不是看有没有回本。**")
    a("")
    a("### 4.3 稳健性检验（滚动窗口）")
    a("")
    a("如果优势是真的，它应该在相邻窗口里反复出现；如果只是某一段的运气，"
      "换个窗口就会消失。下面是同一套规则在多个 40 期窗口上的表现：")
    a("")
    a("| 窗口 | 期号区间 | 后区命中 | 随机期望 | 前区命中 | 回款/成本 |")
    a("|---|---|---|---|---|---|")
    exp_back = n_ticks * BACK_PICK * BACK_PICK / 12
    for i, w in enumerate(roll, 1):
        mark = " ✅" if w["back"] > exp_back else ""
        a(f"| W{i} | {w['start']} – {w['end']} | {w['back']}{mark} | "
          f"{exp_back:.1f} | {w['front']} | {w['money']:.0f}/{w['notes'] * PRICE} |")
    n_up = sum(1 for w in roll if w["back"] > exp_back)
    a("")
    a(f"共 {len(roll)} 个窗口，后区高于期望的有 {n_up} 个。"
      f"相邻窗口重叠一半（步长 20、长度 40），不是独立样本，"
      f"若只有个别窗口突出，应视为噪声而非能力。")
    a("")
    a("### 4.4 参数敏感性（后区）")
    a("")
    sw = sorted(s["hits"] for s in sens)
    a(f"只改后区打分的两个参数（频次权重 0.3–0.7、频次窗口 20–100 期），"
      f"共 {len(sens)} 种组合，盲测后区命中总数范围 **{sw[0]} – {sw[-1]}**"
      f"（中位数 {statistics.median(sw):.0f}，期望 {exp_back:.1f}）。")
    a("")
    hdr = [s for s in sens if s["w"] == sens[0]["w"]]
    a("| 频次权重 \\ 窗口 | " + " | ".join(f"{s['fw']}期" for s in hdr) + " |")
    a("|---" * (1 + len(hdr)) + "|")
    for w in sorted({s["w"] for s in sens}):
        cells = " | ".join(str(s["hits"]) for s in sens if s["w"] == w)
        a(f"| {w:.1f} | {cells} |")
    a("")
    a("> 这张表是这个方案最重要的自检：**如果微小改参就让命中数大幅摆动，"
      "那所谓优势就不是方法带来的，而是参数恰好贴合了这段历史。**")
    a("")
    a("### 4.5 结论")
    a("")
    looks_better = p_back >= 95 or p_front >= 95
    looks_worse = p_back <= 5 or p_front <= 5
    reproduced = n_up > len(roll) / 2
    spread = sw[-1] - sw[0]
    stable = spread <= 0.25 * exp_back
    combo = "**无预测能力**"
    if looks_better and reproduced and stable:
        combo = "**单次与稳健性同向为正，但仍不足以宣称优势**"
    if looks_better:
        a(f"从单次检验看，后区命中总数 {back_hits} 次落在结构一致零分布的第 "
          f"{p_back:.1f} 百分位，前区命中总数 {front_hits} 次在第 {p_front:.1f} 百分位。"
          f"但这**不等于方法有效**，原因有三：")
        a("")
        a(f"1. 40 期只有 {n_ticks} 注，在“试了多种窗口/参数”的前提下，"
          f"单次跳出 2σ 以上很容易撞到；")
        a(f"2. 滚动窗口里只有 {n_up} / {len(roll)} 个高于期望，"
          f"{'优势没有稳定复现' if not reproduced else '多数窗口同向，但仍需更长样本'}；")
        a(f"3. 参数敏感性显示命中总数在 {sw[0]}–{sw[-1]} 之间摆动"
          f"（跨度 {spread}，本次配置"
          f"{'恰在高位' if back_hits >= sw[-1] - 1 else '并非最优'}）。")
        a("")
        a("要继续验证，需把记录累积到 300 期以上，并只认可**所有窗口同时**跑赢的版本。")
    elif looks_worse:
        a("盲测结果至少一项指标显著劣于随机，当前规则没有正向作用，"
          "建议只当作形态分散的选号工具，不要据此加大投入。")
        combo = "**当前规则没有正向作用**"
    else:
        a("**本方案与随机选号无统计差异。** 这是最可能也最诚实的结果：")
        a("大乐透每期独立同分布，任何基于历史号码的筛选都不改变单注 "
          "1/21,425,712 的概率。")
        a("下方注单仅作为形态分散的娱乐性选号，不具备预测能力。")
    a("")
    a(f"**最终判定**：盲测后区命中 {back_hits} 次、前区命中 {front_hits} 次；"
      f"换个窗口只有 {n_up}/{len(roll)} 跑赢期望，"
      f"微调参数命中数在 {sw[0]}–{sw[-1]} 之间摆动。"
      f"综合三项证据，{combo}，"
      f"本报告的价值在于给出可复现的记录方式，而不是号码本身。")
    a("")
    a("## 五、单注概率对照（理论值，由组合数现算）")
    a("")
    total = math.comb(35, 5) * math.comb(12, 2)
    a(f"分母 = C(35,5) × C(12,2) = {math.comb(35, 5):,} × {math.comb(12, 2)} "
      f"= **{total:,}**。")
    a("")
    a("| 奖级 | 中奖条件 | 组合数 | 概率 | 单注奖金 |")
    a("|---|---|---|---|---|")
    cond_text = {
        "一等奖": "5 前 + 2 后",
        "二等奖": "5 前 + 1 后",
        "三等奖": "5 前、4 前 + 2 后",
        "四等奖": "4 前 + 1 后",
        "五等奖": "4 前、3 前 + 2 后",
        "六等奖": "3 前 + 1 后、2 前 + 2 后",
        "七等奖": "3 前、2 前 + 1 后、1 前 + 2 后、2 后",
    }
    fixed_amount = {}
    for cond, (base, _boost) in FIXED_PRIZE.items():
        name = next(n for n, conds in TIERS if cond in conds)
        fixed_amount[name] = base

    for name, cnt, _ in tier_counts():
        if cnt == 1:
            prob = f"1 / {total // cnt:,}"
        else:
            ratio = total / cnt
            prob = (f"1 / {ratio:.1f}" if ratio < 100
                    else f"1 / {ratio:,.0f}")
        if name in fixed_amount:
            amount = f"{fixed_amount[name]:,} 元"
        else:
            amount = "浮动（单注上限 500 万）"
        a(f"| {name} | {cond_text[name]} | {cnt:,} | {prob} | {amount} |")
    win_cnt = sum(cnt for _, cnt, _ in tier_counts())
    a(f"| 任意奖 | — | {win_cnt:,} | 约 1 / {total / win_cnt:.0f}"
      f"（{100 * win_cnt / total:.2f}%） | — |")
    a("")
    a("## 六、下期怎么用")
    a("")
    a("```powershell")
    a("py dlt.py          # 抓最新数据 + 回填上期命中 + 生成新一期报告")
    a("```")
    a("")
    a("同一天重复运行是安全的：`reports/dlt/history.csv` 按期号+注号去重合并，"
      "已开奖的期号会带上命中与奖金，未开奖的保持“待开奖”。")
    a("")
    a("---")
    a("")
    a("**本方案不改变任何单注中奖概率；以上内容为统计分析与娱乐性选号，"
      "请理性购彩，量力而行，未满 18 周岁不得购买彩票。**")
    a("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    log(f"  报告已写入 {path}")


def update_history(path: str, bt_rows: list[dict], tickets: list[dict],
                   code: str, target_date: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                old[(row["期号"], row["注号"])] = row

    for r in bt_rows:
        old[(r["code"], str(r["idx"]))] = {
            "期号": r["code"],
            "日期": r["date"],
            "注号": str(r["idx"]),
            "前区": fmt_nums(r["front"]),
            "后区": fmt_nums(r["back"]),
            "前命中": str(r["hit_front"]),
            "后命中": str(r["hit_back"]),
            "奖级": r["prize"] or "",
            "奖金": f"{r['money']:.0f}",
            "状态": "已开奖",
        }

    for i, t in enumerate(tickets, 1):
        old[(code, str(i))] = {
            "期号": code,
            "日期": target_date,
            "注号": str(i),
            "前区": fmt_nums(t["front"]),
            "后区": fmt_nums(t["back"]),
            "前命中": "",
            "后命中": "",
            "奖级": "",
            "奖金": "",
            "状态": "待开奖",
        }

    fields = ["期号", "日期", "注号", "前区", "后区", "前命中", "后命中",
              "奖级", "奖金", "状态"]
    keys = sorted(old, key=lambda k: (k[0], int(k[1])))
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for k in keys:
            w.writerow(old[k])
    log(f"  记录已更新 {path}（{len(keys)} 条）")


# ---------------------------------------------------------------- 主流程

def main() -> None:
    fit_console()
    ap = argparse.ArgumentParser(description="超级大乐透统计分析 / 选号 / 回测")
    ap.add_argument("--no-fetch", action="store_true", help="只用本地缓存")
    ap.add_argument("--window", type=int, default=WINDOW, help="数据窗口期数")
    ap.add_argument("--test-n", type=int, default=TEST_N, help="盲测期数")
    args = ap.parse_args()

    window = args.window
    test_n = args.test_n

    draws = get_draws(not args.no_fetch)
    if len(draws) < window + 10:
        raise SystemExit(f"数据不足: 需要 {window}+ 期, 实际 {len(draws)}")

    end = len(draws)
    start = end - window
    bt_start = end - test_n
    rule_n = window - test_n

    log(f"[2/6] 数据窗口 {draws[start]['code']} – {draws[-1]['code']}（{window} 期）")
    log(f"      规则期 {rule_n} 期 / 盲测期 {test_n} 期")

    pool = latest_pool(draws)
    table = prize_table(pool)
    log(f"      奖池 {pool:,.0f} 元 -> 固定奖档位 "
        f"{'8 亿以上' if pool >= POOL_BOOST else '8 亿以下'}")

    log("[3/6] 回测（后 %d 期盲测）..." % test_n)
    bt_rows = backtest(draws, bt_start, end, table)
    n = len(bt_rows)
    bh = sum(r["hit_back"] for r in bt_rows)
    fh = sum(r["hit_front"] for r in bt_rows)
    money = sum(r["money"] for r in bt_rows)
    log(f"      {n} 注: 前区命中 {fh}（期望 {n * FRONT_PICK * FRONT_PICK / 35:.1f}），"
        f"后区命中 {bh}（期望 {n * BACK_PICK * BACK_PICK / 12:.1f}），"
        f"固定奖 {money:.0f}/{n * PRICE} 元")

    log(f"[4/6] 蒙特卡洛对照 {MC_TRIALS} 次 ...")
    mc = monte_carlo(draws, bt_start, end, MC_TRIALS)
    log(f"      1σ: 前区 ±{statistics.pstdev(mc['front_hits']):.1f}, "
        f"后区(结构一致) ±{statistics.pstdev(mc['back_match']):.1f}, "
        f"后区(独立) ±{statistics.pstdev(mc['back_ind']):.1f}")

    log("[5/6] 稳健性检验（滚动窗口 + 参数敏感性）...")
    roll = rolling_check(draws, test_n, table)
    exp_back = n * BACK_PICK * BACK_PICK / 12
    up = sum(1 for w in roll if w["back"] > exp_back)
    log(f"      滚动 {len(roll)} 窗口, 后区高于期望 {up}/{len(roll)}: "
        + ", ".join(f"{w['back']}" for w in roll))
    sens = back_sensitivity(draws, bt_start, end)
    sw = sorted(s["hits"] for s in sens)
    log(f"      参数敏感性 {len(sens)} 组, 后区命中 {sw[0]}–{sw[-1]}"
        f" (中位 {statistics.median(sw):.0f}, 期望 {exp_back:.1f})")

    log("[6/6] 生成下一期注单 ...")
    last = draws[-1]
    code, target_date = next_issue(last)
    feat = build_features(draws)
    tickets = select_tickets(feat, last["front"])
    log(f"      目标期 {code}（{target_date}）")
    for i, t in enumerate(tickets, 1):
        log(f"      第{i}注  {fmt_nums(t['front'])}  +  {fmt_nums(t['back'])}")

    log("[7/7] 写报告与记录 ...")
    report_path = os.path.join(REPORT_DIR, f"{code}.md")
    write_report(report_path, code, target_date, tickets, feat, last,
                 bt_rows, mc, roll, sens, pool,
                 draws[start]["code"], bt_start, end)
    update_history(HIST_CSV, bt_rows, tickets, code, target_date)

    p_back = percentile(mc["back_match"], bh)
    p_front = percentile(mc["front_hits"], fh)
    log("")
    log(f"盲测结论: 后区 {bh} 次 第{p_back:.1f}百分位"
        f"({verdict(bh, mc['back_match'])}) | "
        f"前区 {fh} 次 第{p_front:.1f}百分位({verdict(fh, mc['front_hits'])})")
    log(f"稳健性: {up}/{len(roll)} 个窗口后区高于期望")


if __name__ == "__main__":
    main()
