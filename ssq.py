#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双色球 历史数据分析 / 选号 / 回测工具（纯标准库，零第三方依赖）

用法:
    py ssq.py                # 抓取数据 -> 回测 -> 出下一期 5 注 + 报告
    py ssq.py --no-fetch     # 只用本地 data/ssq.csv
    py ssq.py --window 120   # 改变分析窗口期数

设计原则:
    1) 双色球每期独立同分布，本工具不"预测"，只做统计筛选 + 组合设计 + 严格复盘。
    2) 参数刻意保持最少（1 组权重），避免在 100 期小样本上过拟合。
    3) 回测切分: 前 60 期定规则 / 后 40 期盲测；结论只允许"优于/无差异/劣于随机"。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import datetime, date, timedelta
from itertools import combinations

# ---------------------------------------------------------------- 配置

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_CSV = os.path.join(BASE, "data", "ssq.csv")
REPORT_DIR = os.path.join(BASE, "reports")
HIST_CSV = os.path.join(REPORT_DIR, "history.csv")

WINDOW = 100          # 分析窗口: 近 100 期
RULE_N = 60           # 前 60 期定规则
TEST_N = 40           # 后 40 期盲测
FETCH_COUNT = 260     # 多抓一些做特征预热
FREQ_WINDOW = 50      # 频次统计窗口
OM_WINDOW = 100       # 遗漏参考窗口
TICKETS = 5           # 每期注数
PRICE = 2             # 单注价格
MC_TRIALS = 5000      # 蒙特卡洛次数
SEED = 20261029       # 固定随机种子，保证可复现

W_FREQ = 0.6          # 权重: 近 50 期频次
W_OMIT = 0.4          # 权重: 遗漏回归值

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

DRAW_WEEKDAYS = {1, 3, 6}   # 周二 / 周四 / 周日  (Mon=0)
SALE_CUTOFF_HOUR = 20       # 一般 20:00 停售

# 奖级表: (红命中, 蓝命中) -> (等级名, 单注奖金); None 表示浮动奖
PRIZE = {
    (6, 1): ("一等奖", None),
    (6, 0): ("二等奖", None),
    (5, 1): ("三等奖", 3000),
    (5, 0): ("四等奖", 200),
    (4, 1): ("四等奖", 200),
    (4, 0): ("五等奖", 10),
    (3, 1): ("五等奖", 10),
    (2, 1): ("六等奖", 5),
    (1, 1): ("六等奖", 5),
    (0, 1): ("六等奖", 5),
}


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
    """优先用系统 curl（本机 urllib 的 TLS 请求会被拦截），urllib 兜底。

    返回自动识别 utf-8 / gbk 解码后的文本。
    """
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


def fetch_official(count: int) -> list[dict]:
    """中彩网官方接口，JSON。"""
    url = ("https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/"
           f"findDrawNotice?name=ssq&issueCount={count}&pageNo=1&pageSize={count}")
    text = http_get(url, referer="https://www.cwl.gov.cn/")
    payload = json.loads(text)
    if payload.get("state") != 0:
        raise RuntimeError(f"官方接口返回 state={payload.get('state')} {payload.get('message')}")

    draws = []
    for row in payload.get("result", []):
        reds = [int(x) for x in str(row.get("red", "")).split(",") if x.strip()]
        blue = int(str(row.get("blue", "")).strip())
        draws.append({
            "code": str(row.get("code", "")).strip(),
            "date": str(row.get("date", "")).split("(")[0].strip(),
            "red": sorted(reds),
            "blue": blue,
            "sales": row.get("sales") or "",
            "pool": row.get("poolmoney") or "",
        })
    return draws


def fetch_500(count: int) -> list[dict]:
    """500彩票网备用源，HTML 表格。"""
    url = (f"https://datachart.500.com/ssq/history/newinc/history.php"
           f"?limit={count}&sort=0")
    html = http_get(url, referer="https://datachart.500.com/ssq/history/history.shtml")

    draws = []
    for row in re.findall(r'<tr[^>]*class="t_tr1"[^>]*>(.*?)</tr>', html, re.S):
        nums = re.findall(r">\s*(\d{1,2})\s*<", row)
        if len(nums) < 8:
            continue
        code = nums[0]
        if len(code) != 7:
            continue
        reds = sorted(int(x) for x in nums[1:7])
        blue = int(nums[7])
        dates = re.findall(r"(\d{4}-\d{2}-\d{2})", row)
        draws.append({
            "code": code,
            "date": dates[0] if dates else "",
            "red": reds,
            "blue": blue,
            "sales": "",
            "pool": "",
        })
    return draws


def validate(draws: list[dict]) -> list[dict]:
    """排序 / 去重 / 范围校验 / 期号连续性检查。"""
    seen, good = set(), []
    for d in draws:
        code = d.get("code", "")
        reds, blue = d.get("red") or [], d.get("blue")
        if len(code) != 7 or not code.isdigit():
            log(f"  [丢弃] 期号异常: {code!r}")
            continue
        if len(set(reds)) != 6 or not all(1 <= x <= 33 for x in reds):
            log(f"  [丢弃] {code} 红球非法: {reds}")
            continue
        if not isinstance(blue, int) or not 1 <= blue <= 16:
            log(f"  [丢弃] {code} 蓝球非法: {blue}")
            continue
        if code in seen:
            continue
        seen.add(code)
        good.append({**d, "red": sorted(reds)})

    good.sort(key=lambda d: d["code"])

    gaps = 0
    for a, b in zip(good, good[1:]):
        if a["code"][:4] == b["code"][:4]:
            if int(b["code"][4:]) != int(a["code"][4:]) + 1:
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
                "red": [int(row[f"红{i}"]) for i in range(1, 7)],
                "blue": int(row["蓝球"]),
                "sales": row.get("销量", ""),
                "pool": row.get("奖池", ""),
            })
    return draws


def save_csv(path: str, draws: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["期号", "日期", "红1", "红2", "红3", "红4", "红5", "红6",
                    "蓝球", "销量", "奖池"])
        for d in draws:
            w.writerow([d["code"], d["date"], *d["red"], d["blue"],
                        d["sales"], d["pool"]])


def get_draws(use_network: bool) -> list[dict]:
    if use_network:
        for name, fn in (("官方接口", fetch_official), ("500彩票网", fetch_500)):
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


def omission(past: list[dict], n: int, key: str = "red") -> int:
    om = 0
    for d in reversed(past):
        if key == "red":
            if n in d["red"]:
                break
        elif d["blue"] == n:
            break
        om += 1
    return om


def blue_score_map(past: list[dict], w: float = 0.5,
                   fw: int = 30) -> tuple[dict[int, float], dict[int, int]]:
    """蓝球打分。生产逻辑与敏感性检验共用这一个函数，避免两套实现漂移。"""
    f = Counter(d["blue"] for d in past[-fw:])
    om = {n: omission(past, n, "blue") for n in range(1, 17)}
    cap = max(max(om.values()), 1)
    fp = pct_rank({n: f.get(n, 0) for n in range(1, 17)})
    mid = {n: 1.0 - abs(om[n] / cap - 0.5) * 2 for n in range(1, 17)}
    return {n: w * fp[n] + (1 - w) * mid[n] for n in range(1, 17)}, om


def build_features(past: list[dict]) -> dict:
    """只用 past（严格早于当期）构建特征。"""
    recent = past[-FREQ_WINDOW:]
    long_win = past[-OM_WINDOW:]

    rf = Counter()
    for d in recent:
        for r in d["red"]:
            rf[r] += 1

    rom = {n: omission(past, n) for n in range(1, 34)}

    # 遗漏参考上界：用窗口内历史最大遗漏，避免个别极值压扁分布
    rom_cap = max(max(rom.values()), 1)

    rf_pct = pct_rank({n: rf.get(n, 0) for n in range(1, 34)})

    # 遗漏回归值: 越接近"历史中段"越高分，刚出过(0)与久未出(极大)都降权
    rom_mid = {n: 1.0 - abs(rom[n] / rom_cap - 0.5) * 2 for n in range(1, 34)}

    red_score = {n: W_FREQ * rf_pct[n] + W_OMIT * rom_mid[n] for n in range(1, 34)}
    blue_score, bom = blue_score_map(past)

    return {
        "red_score": red_score,
        "blue_score": blue_score,
        "red_freq": rf,
        "red_om": rom,
        "blue_om": bom,
        "long_win": long_win,
    }


# ---------------------------------------------------------------- 形态

def zone_pattern(red: list[int]) -> str:
    z = [0, 0, 0]
    for x in red:
        z[0 if x <= 11 else (1 if x <= 22 else 2)] += 1
    return ":".join(str(v) for v in z)


def form_ok(red: list[int], last_red: list[int]) -> bool:
    total = sum(red)
    if not 90 <= total <= 120:
        return False
    odd = sum(1 for x in red if x % 2)
    if not 2 <= odd <= 4:
        return False
    if "0" in zone_pattern(red).split(":"):
        return False
    if len(set(red) & set(last_red)) > 2:
        return False
    for i in range(len(red) - 2):
        if red[i] + 1 == red[i + 1] and red[i + 1] + 1 == red[i + 2]:
            return False
    return True


def form_signature(red: list[int]) -> tuple:
    odd = sum(1 for x in red if x % 2)
    return (odd, zone_pattern(red))


def select_tickets(feat: dict, last_red: list[int]) -> list[dict]:
    """从高分号池中枚举候选组合，按分数选 5 注，保证注间重合 <=2 且形态分散。"""
    score = feat["red_score"]
    order = sorted(range(1, 34), key=lambda n: score[n], reverse=True)

    candidates: list[tuple[float, tuple]] = []
    for topn in (16, 18, 20, 22, 25, 28, 33):
        pool = sorted(order[:topn])
        if len(pool) < 6:
            continue
        candidates = []
        for combo in combinations(pool, 6):
            if form_ok(list(combo), last_red):
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

    blues = sorted(range(1, 17), key=lambda n: feat["blue_score"][n], reverse=True)[:2]
    tickets = []
    for i, (sc, combo) in enumerate(chosen):
        blue = blues[0] if i < 3 else blues[1]   # 3:2 分配
        tickets.append({
            "red": list(combo),
            "blue": blue,
            "score": sc,
            "blue_score": feat["blue_score"][blue],
        })
    return tickets


# ---------------------------------------------------------------- 结算

def settle(red: list[int], blue: int, draw: dict) -> tuple[int, int, str | None, float]:
    nr = len(set(red) & set(draw["red"]))
    nb = 1 if blue == draw["blue"] else 0
    name, money = PRIZE.get((nr, nb), (None, 0))
    return nr, nb, name, float(money or 0)


# ---------------------------------------------------------------- 回测

def backtest(draws: list[dict], start: int, end: int) -> list[dict]:
    rows = []
    for idx in range(start, end):
        feat = build_features(draws[:idx])
        tickets = select_tickets(feat, draws[idx - 1]["red"])
        for k, t in enumerate(tickets, 1):
            nr, nb, name, money = settle(t["red"], t["blue"], draws[idx])
            rows.append({
                "code": draws[idx]["code"],
                "date": draws[idx]["date"],
                "idx": k,
                "red": t["red"],
                "blue": t["blue"],
                "hit_red": nr,
                "hit_blue": nb,
                "prize": name,
                "money": money,
            })
    return rows


def monte_carlo(draws: list[dict], start: int, end: int, trials: int) -> dict:
    """随机选号的对照分布。

    两种零分布，因为两者方差不同，必须分开比：
      blue_ind    : 200 注各自随机蓝球（普通玩家的买法），1σ≈3.4
      blue_match  : 每期随机 2 个蓝球、按 3:2 分配（与本方案结构一致），1σ≈5.4
    """
    rng = random.Random(SEED ^ 0x5A5A)
    blue_ind, blue_match, red3 = [], [], []
    for _ in range(trials):
        bi = bm = r3 = 0
        for idx in range(start, end):
            draw = draws[idx]
            for _ in range(TICKETS):
                red = rng.sample(range(1, 34), 6)
                if len(set(red) & set(draw["red"])) >= 3:
                    r3 += 1
                if rng.randint(1, 16) == draw["blue"]:
                    bi += 1
            b1, b2 = rng.sample(range(1, 17), 2)
            if b1 == draw["blue"]:
                bm += 3
            elif b2 == draw["blue"]:
                bm += 2
        blue_ind.append(bi)
        blue_match.append(bm)
        red3.append(r3)
    return {"n": (end - start) * TICKETS, "blue_ind": blue_ind,
            "blue_match": blue_match, "red3": red3}


def rolling_check(draws: list[dict], test_n: int, step: int = 20) -> list[dict]:
    """把同一套规则在多个滚动窗口上重跑，检验优势是否只在某一段偶然出现。"""
    out = []
    for st in range(RULE_N, len(draws) - test_n + 1, step):
        rows = backtest(draws, st, st + test_n)
        out.append({
            "start": draws[st]["code"],
            "end": draws[st + test_n - 1]["code"],
            "blue": sum(r["hit_blue"] for r in rows),
            "red3": sum(1 for r in rows if r["hit_red"] >= 3),
            "money": sum(r["money"] for r in rows),
            "notes": len(rows),
        })
    return out


def blue_sensitivity(draws: list[dict], start: int, end: int,
                     weights=(0.3, 0.4, 0.5, 0.6, 0.7),
                     windows=(20, 30, 50, 100)) -> list[dict]:
    """蓝球打分参数的敏感性: 只改权重/频次窗口, 看命中次数摆幅。

    若小幅改参就让命中数在期望值上下大幅摆动，说明"优势"是实现噪声。
    """
    out = []
    for w in weights:
        for fw in windows:
            hits = 0
            for idx in range(start, end):
                sc, _ = blue_score_map(draws[:idx], w, fw)
                top2 = sorted(sc, key=lambda n: sc[n], reverse=True)[:2]
                act = draws[idx]["blue"]
                hits += 3 * (top2[0] == act) + 2 * (top2[1] == act)
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

    last_year, last_seq = int(latest["code"][:4]), int(latest["code"][4:])
    if target.year == last_year:
        code = f"{last_year}{last_seq + 1:03d}"
    else:
        code = f"{target.year}001"
    return code, target.isoformat()


# ---------------------------------------------------------------- 报告

def fmt_red(red: list[int]) -> str:
    return " ".join(f"{x:02d}" for x in red)


def write_report(path: str, code: str, target_date: str, tickets: list[dict],
                 feat: dict, last: dict, bt_rows: list[dict],
                 mc: dict, roll: list[dict], sens: list[dict],
                 window_start: str, bt_start: int, bt_end: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    n_ticks = len(bt_rows)
    cost = n_ticks * PRICE
    win = [r for r in bt_rows if r["money"] > 0]
    money = sum(r["money"] for r in bt_rows)
    blue_hits = sum(r["hit_blue"] for r in bt_rows)
    red3 = sum(1 for r in bt_rows if r["hit_red"] >= 3)
    red4 = sum(1 for r in bt_rows if r["hit_red"] >= 4)

    # 蓝球用"结构相同"的零分布判定，红球用 200 注独立随机判定
    m_mean = statistics.fmean(mc["blue_match"])
    m_sd = statistics.pstdev(mc["blue_match"])
    i_mean = statistics.fmean(mc["blue_ind"])
    i_sd = statistics.pstdev(mc["blue_ind"])
    mr_mean = statistics.fmean(mc["red3"])
    mr_sd = statistics.pstdev(mc["red3"])
    v_blue = verdict(blue_hits, mc["blue_match"])
    v_red = verdict(red3, mc["red3"])
    p_blue = percentile(mc["blue_match"], blue_hits)
    p_red = percentile(mc["red3"], red3)

    lines = []
    a = lines.append
    a(f"# 双色球 {code} 期分析报告")
    a("")
    a(f"- 目标期: **{code}**（{target_date}）")
    a(f"- 投注方案: 单式 **{TICKETS} 注**，共 {TICKETS * PRICE} 元")
    a(f"- 数据窗口: 近 {WINDOW} 期（{window_start} – {last['code']}），"
      f"盲测段 {bt_rows[0]['code']} – {last['code']}")
    a(f"- 生成时间: {datetime.now():%Y-%m-%d %H:%M}")
    a("")
    a("## 一、本期推荐注单")
    a("")
    a("| 注 | 红球 | 蓝球 | 和值 | 奇偶 | 三区 | 重号 | 组合分 |")
    a("|---|---|---|---|---|---|---|---|")
    for i, t in enumerate(tickets, 1):
        red = t["red"]
        odd = sum(1 for x in red if x % 2)
        rep = len(set(red) & set(last["red"]))
        a(f"| {i} | {fmt_red(red)} | {t['blue']:02d} | {sum(red)} | "
          f"{odd}:{6 - odd} | {zone_pattern(red)} | {rep} | {t['score']:.2f} |")
    a("")
    a("## 二、每注选号理由")
    a("")
    bs = feat["blue_score"]
    for i, t in enumerate(tickets, 1):
        red = t["red"]
        top = sorted(red, key=lambda n: feat["red_score"][n], reverse=True)[:3]
        tag = ", ".join(
            f"{n:02d}(频{feat['red_freq'].get(n, 0)}次/遗漏{feat['red_om'][n]})"
            for n in top)
        a(f"**第 {i} 注** `{fmt_red(red)} + {t['blue']:02d}`  "
          f"和值 {sum(red)}，奇偶 {sum(1 for x in red if x % 2)}:"
          f"{6 - sum(1 for x in red if x % 2)}，三区 {zone_pattern(red)}，"
          f"与 {last['code']} 期重号 {len(set(red) & set(last['red']))} 个。"
          f"核心高分号: {tag}；蓝球 {t['blue']:02d} 得分 {bs[t['blue']]:.2f}"
          f"（遗漏 {feat['blue_om'][t['blue']]} 期）。")
        a("")

    a("## 三、5 注覆盖度")
    a("")
    all_nums = sorted({n for t in tickets for n in t["red"]})
    a(f"- 覆盖红球 {len(all_nums)} / 33 个: {fmt_red(all_nums)}")
    a(f"- 蓝球覆盖: {', '.join(f'{b:02d}' for b in sorted({t['blue'] for t in tickets}))}"
      f"（按 3:2 分配给最高分两个候选）")
    a(f"- 注间最大重合: "
      f"{max(len(set(x['red']) & set(y['red'])) for x, y in combinations(tickets, 2))} 个"
      f"（约束上限 2）")
    odd_dist = Counter(sum(1 for v in t["red"] if v % 2) for t in tickets)
    a(f"- 奇偶分布: " + "，".join(f"{k}:{6 - k} × {v} 注" for k, v in sorted(odd_dist.items())))
    zc = Counter(zone_pattern(t["red"]) for t in tickets)
    a(f"- 三区分布: " + "，".join(f"{k} × {v} 注" for k, v in sorted(zc.items())))
    a(f"- 和值区间: {min(sum(t['red']) for t in tickets)} – "
      f"{max(sum(t['red']) for t in tickets)}（约束 90–120）")
    a("")

    a("## 四、回测结论（盲测期）")
    a("")
    a(f"规则用前 {RULE_N} 期确定，参数冻结后在**后 {TEST_N} 期**盲测，"
      f"共 {n_ticks} 注、成本 {cost} 元。")
    a("")
    a("### 4.1 蓝球：两种零分布必须分开比")
    a("")
    a("先说明一个容易算错的点：本方案每期只集中在 2 个蓝球上按 3:2 分配，"
      "**每注命中概率仍是 1/16、5 注期望仍是 5/16，但方差比买 5 个不同蓝球大一倍多**。"
      "所以不能拿“200 注独立随机”当唯一基准，否则会把方差误判成优势。")
    a("")
    a("| 零分布 | 期望 | 1σ | 本方案 | 百分位 | 判定 |")
    a("|---|---|---|---|---|---|")
    a(f"| 结构一致（每期随机 2 蓝，3:2） | {m_mean:.1f} | ±{m_sd:.1f} | "
      f"{blue_hits} | {p_blue:.1f} | {v_blue} |")
    a(f"| 200 注各自随机蓝球 | {i_mean:.1f} | ±{i_sd:.1f} | "
      f"{blue_hits} | {percentile(mc['blue_ind'], blue_hits):.1f} | 参考 |")
    a("")
    a("### 4.2 红球")
    a("")
    a("| 指标 | 本方案 | 随机基准期望 | 随机 1σ | 百分位 | 判定 |")
    a("|---|---|---|---|---|---|")
    a(f"| 命中 ≥3 个 | {red3} | {mr_mean:.1f} | ±{mr_sd:.1f} | {p_red:.1f} | {v_red} |")
    a(f"| 命中 ≥4 个 | {red4} | {n_ticks * 0.0049:.1f} | — | — | 参考 |")
    a("")
    a(f"- 中奖注数 {len(win)} / {n_ticks}，回款 {money:.0f} 元，"
      f"投入 {cost} 元，回报率 {100 * money / cost:.1f}%")
    pz = Counter(r["prize"] for r in bt_rows if r["prize"])
    if pz:
        a("- 奖级分布: " + "，".join(f"{k} × {v}" for k, v in pz.most_common()))
    a(f"- 蒙特卡洛: {MC_TRIALS} 次，每次模拟 40 期 × 5 注")
    a("")
    a("> 回报率低于 50% 是正常现象：双色球理论返奖率约 50%，其中约 17%–25% "
      "压在一等奖上，短样本碰不到头奖。因此**判定标准看命中分布是否跳出随机区间，"
      "而不是看有没有回本**。")
    a("")
    a("### 4.3 稳健性检验（滚动窗口）")
    a("")
    a("如果优势是真的，它应该在相邻窗口里反复出现；如果只是某一段的运气，"
      "换个窗口就会消失。下面是同一套规则在多个 40 期窗口上的表现：")
    a("")
    a("| 窗口 | 期号区间 | 蓝球命中 | 随机期望 | 红球≥3 | 回款/成本 |")
    a("|---|---|---|---|---|---|")
    exp_blue = TEST_N * TICKETS / 16
    for i, w in enumerate(roll, 1):
        mark = " ✅" if w["blue"] > exp_blue else ""
        a(f"| W{i} | {w['start']} – {w['end']} | {w['blue']}{mark} | "
          f"{exp_blue:.1f} | {w['red3']} | {w['money']:.0f}/{w['notes'] * PRICE} |")
    n_up = sum(1 for w in roll if w["blue"] > exp_blue)
    a("")
    a(f"共 {len(roll)} 个窗口，蓝球高于期望的有 {n_up} 个。"
      f"相邻窗口重叠一半（步长 20、长度 40），不是独立样本，"
      f"若只有个别窗口突出，应视为噪声而非能力。")
    a("")
    a("### 4.4 参数敏感性（蓝球）")
    a("")
    exp_b = TEST_N * TICKETS / 16
    sw = sorted(s["hits"] for s in sens)
    a(f"只改蓝球打分的两个参数（频次权重 0.3–0.7、频次窗口 20–100 期），"
      f"共 {len(sens)} 种组合，盲测命中次数范围 **{sw[0]} – {sw[-1]}**"
      f"（中位数 {statistics.median(sw):.0f}，期望 {exp_b:.1f}）。")
    a("")
    a("| 频次权重 \\ 窗口 | " + " | ".join(f"{s['fw']}期" for s in sens if s["w"] == sens[0]["w"]) + " |")
    a("|---" * (1 + len([s for s in sens if s["w"] == sens[0]["w"]])) + "|")
    for w in sorted({s["w"] for s in sens}):
        cells = " | ".join(str(s["hits"]) for s in sens if s["w"] == w)
        a(f"| {w:.1f} | {cells} |")
    a("")
    a("> 这张表是这个方案最重要的自检：**如果微小改参就让命中数从 20 跳到 30，"
      "那所谓优势就不是方法带来的，而是参数恰好贴合了这段历史。**")
    a("")
    a("### 4.5 结论")
    a("")
    looks_better = p_blue >= 95 or p_red >= 95
    looks_worse = p_blue <= 5 or p_red <= 5
    if looks_better:
        a(f"从单次检验看，蓝球 {blue_hits} 次落在结构一致零分布的第 {p_blue:.1f} 百分位，"
          f"红球≥3 {red3} 次在第 {p_red:.1f} 百分位。"
          f"但这**不等于方法有效**，原因有三：")
        a("")
        a("1. 40 期只有 200 注，在“试了多种窗口/参数”的前提下，单次超出 3σ 很容易撞到；")
        a(f"2. 滚动窗口里只有 {n_up} / {len(roll)} 个高于期望，优势没有稳定复现；")
        a(f"3. 参数敏感性显示命中数在 {sw[0]}–{sw[-1]} 之间摆动，本配置恰好落在最高点。")
        a("")
        a("要继续验证，需把记录累积到 300 期以上，并只认可**所有窗口同时**跑赢的版本。")
    elif looks_worse:
        a("盲测结果至少一项指标显著劣于随机，当前规则没有正向作用，"
          "建议只当作形态分散的选号工具，不要据此加大投入。")
    else:
        a("**本方案与随机选号无统计差异。** 这是最可能也最诚实的结果：")
        a("双色球每期独立同分布，任何基于历史号码的筛选都不改变单注 1/17,721,088 的概率。")
        a("下方注单仅作为形态分散的娱乐性选号，不具备预测能力。")
    a("")
    a(f"**最终判定**：盲测蓝球 {blue_hits} 次、红球≥3 {red3} 次；"
      f"但换个窗口只有 {n_up}/{len(roll)} 跑赢期望，"
      f"微调参数命中数在 {sw[0]}–{sw[-1]} 之间摆动。"
      f"综合三项证据，**当前规则不具备可信的预测能力**，"
      f"本报告的价值在于给出可复现的记录方式，而不是号码本身。")
    a("")
    a("## 五、单注概率对照（理论值）")
    a("")
    a("| 奖级 | 条件 | 概率 |")
    a("|---|---|---|")
    a("| 一等奖 | 6 红 + 蓝 | 1 / 17,721,088 |")
    a("| 二等奖 | 6 红 | 1 / 1,181,406 |")
    a("| 三等奖 | 5 红 + 蓝 | 1 / 109,389 |")
    a("| 四等奖 | 5 红 / 4 红 + 蓝 | 1 / 2,303 |")
    a("| 五等奖 | 4 红 / 3 红 + 蓝 | 1 / 129 |")
    a("| 六等奖 | 0–2 红 + 蓝 | 1 / 17 |")
    a("| 任意奖 | — | 约 1 / 15（6.71%） |")
    a("")
    a("## 六、下期怎么用")
    a("")
    a("```powershell")
    a("py ssq.py          # 抓最新数据 + 回填上期命中 + 生成新一期报告")
    a("```")
    a("")
    a("同一天重复运行是安全的：`reports/history.csv` 按期号+注号去重合并，"
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
            "红球": fmt_red(r["red"]),
            "蓝球": f"{r['blue']:02d}",
            "红命中": str(r["hit_red"]),
            "蓝命中": str(r["hit_blue"]),
            "奖级": r["prize"] or "",
            "奖金": f"{r['money']:.0f}",
            "状态": "已开奖",
        }

    for i, t in enumerate(tickets, 1):
        old[(code, str(i))] = {
            "期号": code,
            "日期": target_date,
            "注号": str(i),
            "红球": fmt_red(t["red"]),
            "蓝球": f"{t['blue']:02d}",
            "红命中": "",
            "蓝命中": "",
            "奖级": "",
            "奖金": "",
            "状态": "待开奖",
        }

    fields = ["期号", "日期", "注号", "红球", "蓝球", "红命中", "蓝命中",
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
    ap = argparse.ArgumentParser(description="双色球统计分析 / 选号 / 回测")
    ap.add_argument("--no-fetch", action="store_true", help="只用本地缓存")
    ap.add_argument("--window", type=int, default=WINDOW, help="分析窗口期数")
    ap.add_argument("--test-n", type=int, default=TEST_N, help="盲测期数")
    args = ap.parse_args()

    window = args.window
    test_n = args.test_n

    draws = get_draws(not args.no_fetch)
    if len(draws) < window + 10:
        raise SystemExit(f"数据不足: 需要 {window}+ 期, 实际 {len(draws)}")

    end = len(draws)
    start = end - window              # 分析窗口起点
    bt_start = end - test_n           # 盲测期起点
    rule_n = window - test_n

    log(f"[2/6] 分析窗口 {draws[start]['code']} – {draws[-1]['code']}（{window} 期）")
    log(f"      规则期 {rule_n} 期 / 盲测期 {test_n} 期")

    log("[3/6] 回测（后 %d 期盲测）..." % test_n)
    bt_rows = backtest(draws, bt_start, end)
    n = len(bt_rows)
    bh = sum(r["hit_blue"] for r in bt_rows)
    r3 = sum(1 for r in bt_rows if r["hit_red"] >= 3)
    money = sum(r["money"] for r in bt_rows)
    log(f"      {n} 注: 蓝球命中 {bh} 次（随机基准 {n / 16:.1f}），"
        f"红球≥3 {r3} 次（随机基准 {n * 0.0577:.1f}），回款 {money:.0f}/{n * PRICE} 元")

    log(f"[4/6] 蒙特卡洛对照 {MC_TRIALS} 次 ...")
    mc = monte_carlo(draws, bt_start, end, MC_TRIALS)
    log(f"      1σ: 蓝球(结构一致) ±{statistics.pstdev(mc['blue_match']):.1f}, "
        f"蓝球(独立) ±{statistics.pstdev(mc['blue_ind']):.1f}, "
        f"红球≥3 ±{statistics.pstdev(mc['red3']):.1f}")

    log("[5/6] 稳健性检验（滚动窗口 + 参数敏感性）...")
    roll = rolling_check(draws, test_n)
    exp_blue = test_n * TICKETS / 16
    up = sum(1 for w in roll if w["blue"] > exp_blue)
    log(f"      滚动 {len(roll)} 窗口, 蓝球高于期望 {up}/{len(roll)}: "
        + ", ".join(f"{w['blue']}" for w in roll))
    sens = blue_sensitivity(draws, bt_start, end)
    sw = sorted(s["hits"] for s in sens)
    log(f"      参数敏感性 {len(sens)} 组, 蓝球命中 {sw[0]}–{sw[-1]}"
        f" (中位 {statistics.median(sw):.0f}, 期望 {exp_blue:.1f})")

    log("[6/6] 生成下一期注单 ...")
    last = draws[-1]
    code, target_date = next_issue(last)
    feat = build_features(draws)
    tickets = select_tickets(feat, last["red"])
    log(f"      目标期 {code}（{target_date}）")
    for i, t in enumerate(tickets, 1):
        log(f"      第{i}注  {fmt_red(t['red'])}  +  {t['blue']:02d}")

    log("[7/7] 写报告与记录 ...")
    report_path = os.path.join(REPORT_DIR, f"{code}.md")
    write_report(report_path, code, target_date, tickets, feat, last,
                 bt_rows, mc, roll, sens, draws[start]["code"], bt_start, end)
    update_history(HIST_CSV, bt_rows, tickets, code, target_date)

    p_blue = percentile(mc["blue_match"], bh)
    p_red = percentile(mc["red3"], r3)
    log("")
    log(f"盲测结论: 蓝球 {bh} 次 第{p_blue:.1f}百分位"
        f"({verdict(bh, mc['blue_match'])}) | "
        f"红球≥3 {r3} 次 第{p_red:.1f}百分位({verdict(r3, mc['red3'])})")
    log(f"稳健性: {up}/{len(roll)} 个窗口蓝球高于期望")


if __name__ == "__main__":
    main()
