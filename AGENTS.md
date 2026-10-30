# AGENTS.md

本文件面向在此仓库工作的编码 agent。仓库有两个**互相独立**的单文件工具，共用同一套
「抓取 → 特征 → 选号 → 盲测 → 复盘」骨架，数据与产物分开放：

| 脚本 | 玩法 | 数据 | 报告 | 记录 |
|---|---|---|---|---|
| `ssq.py` | 双色球：红 33 选 6 + 蓝 16 选 1，2 元 | `data/ssq.csv` | `reports/<期号>.md` | `reports/history.csv` |
| `dlt.py` | 大乐透：前区 35 选 5 + 后区 12 选 2，2 元 | `data/dlt.csv` | `reports/dlt/<期号>.md` | `reports/dlt/history.csv` |

## 先读这几句

1. **这不是预测工具**。两个玩法都是每期独立同分布，工具的价值是「可复现的记录 + 可证伪的自我检验」。
   任何以「提高命中率」为目标的改动（加特征、调参、网格搜索）都与项目定位冲突。
2. **两边都不支持「有预测能力」**：双色球是三项证据互相矛盾（蓝球单次 99.6 百分位，
   但只有 5/9 窗口跑赢、微调参数摆到 8–28）；大乐透是与随机无统计差异
   （后区第 75.0、前区第 24.3 百分位）。依据见 `reports/2026125.md`、`reports/dlt/26124.md`
   第四章。除非三项证据同时反转，不得把结论改写成正面表述。
3. 决策记录与踩坑在 `.dsh/skills/ssq-lottery/references/` 与 `.dsh/skills/dlt-lottery/references/`，
   **改代码前必读**；大乐透的坑（新奖级、5 位期号、21:00 停售）与双色球完全不同，不要照抄。

## 环境与命令

Windows + PowerShell，`py` = Python 3.15（要求 3.10+），`curl.exe` 8.13 可用，**零第三方依赖**。

```powershell
$env:PYTHONIOENCODING="utf-8"     # 控制台是 GBK，中文输出必须先设

py ssq.py                          # 双色球：联网抓 260 期 -> 出下一期 5 注
py ssq.py --no-fetch               # 只用 data/ssq.csv
py ssq.py --window 120 --test-n 40 # 改窗口与盲测期数

py dlt.py                          # 大乐透：联网抓 260 期 -> 出下一期 5 注
py dlt.py --no-fetch               # 只用 data/dlt.csv
py dlt.py --window 120 --test-n 40
```

离线全流程各约 **25 秒**（5000 次蒙特卡洛 + 20 组参数敏感性是主要开销）。

### 运行有副作用，验证时别污染项目

每次运行都会写自己的数据、报告与记录（见上表）。验证改动请复制到临时目录跑：

```powershell
$tmp = Join-Path $env:TEMP "lot_smoke"; New-Item -ItemType Directory $tmp -Force | Out-Null
Copy-Item ssq.py,dlt.py,README.md $tmp; Copy-Item data,reports $tmp -Recurse
$env:PYTHONIOENCODING="utf-8"; py "$tmp\ssq.py" --no-fetch; py "$tmp\dlt.py" --no-fetch
```

离线跑当前数据应复现：双色球 2026125 的 5 注（`06 13 14 24 25 30+15` 等）、
大乐透 26124 的 5 注（`03 10 16 26 27+04 07` 等）。这是回归基线。

## 代码地图

`ssq.py`（900 行）：

| 行 | 区块 | 关键函数 |
|---|---|---|
| 34–73 | 配置常量 | 窗口/权重/种子/`PRIZE` |
| 90–258 | 抓取与数据 | `http_get`、`fetch_official`、`fetch_500`、`validate`、`load_csv`/`save_csv` |
| 263–325 | 特征与打分 | `pct_rank`、`omission`、`blue_score_map`、`build_features` |
| 330–418 | 形态与选号 | `zone_pattern`、`form_ok`、`form_signature`、`select_tickets` |
| 423–533 | 结算与验证 | `settle`、`backtest`、`monte_carlo`、`rolling_check`、`blue_sensitivity` |
| 538–555 | 目标期 | `next_issue` |
| 560–821 | 产物 | `write_report`、`update_history` |
| 826–900 | 主流程 | `main` |

`dlt.py`（1032 行）：

| 行 | 区块 | 关键函数 |
|---|---|---|
| 41–108 | 配置常量 | 窗口/权重/种子/`FIXED_PRIZE`/`TIERS` |
| 110–298 | 抓取与数据 | `http_get`、`fetch_official`（体彩，分页 100）、`fetch_500`、`validate` |
| 300–351 | 特征与打分 | `pct_rank`、`omission`、`back_score_map`、`build_features` |
| 353–452 | 形态与选号 | `zone_pattern`、`form_ok`、`back_pairs`、`select_tickets` |
| 454–596 | 结算与验证 | `prize_table`、`settle`、`backtest`、`monte_carlo`、`rolling_check`、`back_sensitivity` |
| 598–618 | 目标期 | `next_issue` |
| 620–947 | 产物 | `combo_count`、`tier_counts`、`write_report`、`update_history` |
| 949–1032 | 主流程 | `main` |

## 数据契约（改格式要同步多处）

| 文件 | 字段 |
|---|---|
| `data/ssq.csv` | `期号,日期,红1..红6,蓝球,销量,奖池`；期号 7 位；260 期滚动；`utf-8-sig` |
| `data/dlt.csv` | `期号,日期,前1..前5,后1,后2,销量,奖池`；期号 **5 位**；260 期滚动；`utf-8-sig` |
| `reports/history.csv` | `期号,日期,注号,红球,蓝球,红命中,蓝命中,奖级,奖金,状态` |
| `reports/dlt/history.csv` | `期号,日期,注号,前区,后区,前命中,后命中,奖级,奖金,状态` |

历史记录都按 `(期号, 注号)` 去重合并，状态为 `待开奖` / `已开奖`；号码字符串空格分隔、两位补零。
`reports/**/*.md` 与 `data/*.csv` 都是产物，**不要手改**；要改内容就改 `write_report()` / `save_csv()`。

## 硬性约束（破坏其中一条即为回归）

1. **零分布只认结构一致的那个**：双色球用 `blue_match`（每期 2 蓝按 3:2，1σ≈5.3）；
   大乐透用 `back_match`（每期 3 后区按 2/2/1，1σ≈12.5）。独立随机分布只作参考。
   用错基准会把方差当优势——两个项目都踩过。
2. **打分只有一份实现**：`blue_score_map` / `back_score_map` 同时服务生产与敏感性检验。
   出现第二份归一化逻辑就会漂移（双色球曾因此算出 28 与 20 两个结果）。
3. **只用一组权重**（`W_FREQ=0.6` / `W_OMIT=0.4`），不做网格搜索。参数敏感性只用于**证伪**。
4. **特征严格只用 `draws[:idx]`**，禁止让当期或未来数据进入 `build_features`。
5. **概率必须由组合数现算**：双色球分母 `C(33,6)×16`，大乐透 `C(35,5)×C(12,2) = 21,425,712`。
   大乐透奖级是 **2026-01-31 起的 7 档新规则**（三等 5+0/4+2 …），别用旧 9 档表。
6. **百分位用 `.1f`**，不要 `.0f`（99.9 会变成 100）。
7. **目标期号按「最新期号 +1」推算**，不写死；双色球周二/四/日、20:00 停售，
   大乐透周一/三/六、**21:00 停售**，开奖时间不同。
8. **结论口径**：盲测百分位 / 滚动窗口 / 参数敏感性三者矛盾时，必须写「不具备可信的预测能力」。
   禁止美化回测、禁止承诺中奖；报告结尾免责声明必须保留。
9. **纯标准库**：不引入 pandas/numpy/requests。抓取 `curl.exe` 优先、`urllib` 兜底。
10. **可复现**：`SEED` 固定，随机只用局部 `random.Random` 实例。
11. **编码**：控制台 `PYTHONIOENCODING=utf-8`（或 `fit_console()`），报告 `utf-8`，CSV `utf-8-sig`。
12. **两个工具不许互相 import 或共享数据文件**；复制代码时同步改常量与文案，
    别留下双色球的 20:00 / 7 位期号 / 9 奖级。

## 代码风格

- Python 3.10+，`from __future__ import annotations`，公共函数带类型标注与中文 docstring。
- 4 空格缩进、无 tab、行宽 ≤ 99；分节用 `# ---- 区块名` 注释。
- 副作用集中在 `main` / `write_report` / `update_history`；特征与打分保持纯函数。
- 注释解释「为什么」（判定口径、统计选择），不要删这类解释性注释。
- 日志统一走 `log()` 与 `[n/m]` 步骤号；新增步骤要顺延编号。

## 修改后自检

```powershell
py -m py_compile ssq.py dlt.py
py ssq.py --no-fetch   # 临时副本里跑
py dlt.py --no-fetch
```

- 双色球 5 注：和值 90–120、奇偶 2:4–4:2、三区不空、与上期重号 0–2、无三连号、注间重合 ≤2。
- 大乐透 5 注：和值 70–110、奇偶 1:4–4:1、三区（1–12/13–24/25–35）不空、
  与上期重号 0–2、无三连号、注间重合 ≤2；后区 3 个号按 2/2/1 组成 5 对。
- 报告章节齐全，第四章三项证据与最终判定自洽；大乐透概率表由 `tier_counts()` 生成。
- `history.csv` 重复运行不增行；已开奖期号被回填、未开奖保持「待开奖」。
- 单工具全流程 < 60 秒；不要新增项目内的临时文件。

## 已知状况与坑

- **只有本地 git 仓库（无远端）、无测试、无 CI、无 requirements**。`data/*.csv` 与 `reports/`
  是刻意入库的产物（缓存可复现、`history.csv` 是累计记录），改动会带来 diff 噪音，属预期。
- `--window` 只影响数据量校验与窗口起点日志；`--test-n` 才真正改变盲测段。
  报告正文里的「近 100 期 / 前 60 期 / 后 40 期 / 40 期 × 5 注」来自常量
  `WINDOW`、`RULE_N`、`TEST_N`，用参数改动后这些文字会与实际不符，记得一并处理。
- `--no-fetch` 不校验日历：双色球缓存到 2026124（2026-10-29），大乐透缓存到 26123
  （2026-10-28）；各自的下一期（2026125 / 26124）已在 `history.csv` 里标为「待开奖」，
  离线跑仍会重算该期。
- 滚动窗口数随数据长度变化（当前两边都是 9 个），不要在任何文案里写死。
- 大乐透中奖注数会少于双色球（七等奖 1/16.6，双色球六等奖 1/17），
  回测回报率天然偏低，不是 bug。
- `build_features`（ssq）返回的 `long_win` 未被使用；属无害遗留，不必顺手“清理”。
- **项目级 skill 在 `.dsh/skills/`**：DSH 把它当作项目根（`project-dsh`，rank 100），
  一层深度扫描 `<name>/SKILL.md`。frontmatter 的 `name` 必须满足 `^[a-z0-9]+(?:-[a-z0-9]+)*$`，
  否则会被**静默丢弃**。当前 skill 名：`ssq-lottery`、`dlt-lottery`，中文标题只能放
  `description` 与正文；改完 `SKILL.md` 目录会热重载。
- 改动结论或关键数字后，同步更新对应 `references/context.md` 的「关键数字 / 当前结论」两节。

## 禁止事项

- 不为了「更准」而加特征、调权重、搜参数、上复式/胆拖/追加。
- 不引入第三方依赖，不改写成 Web 服务或多文件框架。
- 不手改产物美化结果，不删除或弱化风险提示。
- 不把「单次跳出 2σ/3σ」单独说成方法有效——必须同时给出滚动窗口与敏感性结果。
