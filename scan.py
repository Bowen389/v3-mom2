import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from math import floor, isfinite
from pathlib import Path
from time import sleep

import akshare as ak
import pandas as pd


def fetch(fn, **kwargs):
    for attempt in range(3):
        try:
            return fn(**kwargs)
        except Exception:
            if attempt == 2:
                raise
            sleep(2 ** attempt)


def history(code, start, end, adjust):
    market = "sh" if code.startswith("6") else "sz"
    if code.startswith(("4", "8", "92")):
        market = "bj"
    try:
        rows = fetch(ak.stock_zh_a_hist_tx, symbol=market + code,
                     start_date=start, end_date=end, adjust=adjust)
    except Exception as exc:
        raise RuntimeError(f"{code}: Tencent history request failed ({adjust or 'raw'})") from exc
    rows = rows.rename(columns={
        "date": "日期", "open": "开盘", "close": "收盘",
        "high": "最高", "low": "最低",
    })
    if rows.empty:
        raise ValueError(f"{code}: empty price history")
    rows["日期"] = pd.to_datetime(rows["日期"]).dt.date
    return rows.sort_values("日期").set_index("日期")


def rank(pool, bars, day):
    candidates = []
    for code, name in pool:
        frame = bars[code].loc[:day]
        if frame.empty or frame.index[-1] != day:
            raise ValueError(f"{code}: missing signal-date bar {day}")
        if len(frame) < 61 or (day - frame.index[0]).days < 60:
            continue
        row = frame.iloc[-1]
        ratios = [float(row["收盘"] / frame["收盘"].iloc[-1-n] - 1)
                  for n in (1, 20, 60)]
        if not all(map(isfinite, ratios)):
            raise ValueError(f"{code}: invalid momentum on {day}")
        if ratios[0] >= .099 and row["最高"] != row["最低"]:
            candidates.append((code, name, *ratios[1:]))
    frame = pd.DataFrame(candidates, columns=["code", "name", "ret20", "ret60"])
    frame["score"] = 0.0
    for col in ("ret20", "ret60"):
        sd = frame[col].std(ddof=0)
        if sd > 0:
            frame["score"] += (frame[col] - frame[col].mean()) / sd
    return frame.sort_values(["score", "code"], ascending=[False, True]).head(3)


def simulate(signal, frame, sessions, idx):
    if idx + 1 >= len(sessions):
        return {"status": "pending_entry"}
    buy = sessions[idx + 1]
    result = {"buy_date": str(buy)}
    if buy not in frame.index:
        return result | {"status": "no_buy_bar"}
    entry = frame.loc[buy]
    prev = frame.loc[:sessions[idx], "收盘"].iloc[-1]
    if entry["最低"] == entry["最高"] and entry["开盘"] / prev >= 1.099:
        return result | {"status": "buy_locked"}
    px = float(entry["开盘"])
    if not isfinite(px) or px <= 0:
        raise ValueError(f"{signal.code}: invalid buy open {buy}")
    shares = floor(7500 / px / 100) * 100
    result |= {"buy_open": px, "shares": shares}
    if not shares:
        return result | {"status": "below_one_lot"}
    sell_days = set(sessions[idx + 2:])
    exits = [d for d in frame.index if d in sell_days]
    if not exits:
        return result | {"status": "pending_exit"}
    sell = exits[0]
    out = float(frame.at[sell, "开盘"])
    # Original report gives one slippage figure without specifying a side.
    gross_buy, gross_sell = shares * px, shares * out
    debit = gross_buy + max(5, gross_buy * .0003) + gross_buy * .00001
    credit = gross_sell - max(5, gross_sell * .0008) - gross_sell * .00101
    pnl = credit - debit - gross_buy * .005
    return result | {"status": "closed", "sell_date": str(sell),
                     "sell_open": out, "net_pnl": round(pnl, 2),
                     "net_return": round(pnl / gross_buy, 6)}


STATUS = {
    "closed": "已平仓", "pending_exit": "待卖出", "pending_entry": "待买入",
    "no_buy_bar": "无买入行情", "buy_locked": "涨停锁单", "below_one_lot": "预算不足一手",
}


def number(value, spec):
    return format(value, spec) if value is not None else "—"


def table_row(*cells):
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def write_markdown(directory, reports, summary):
    daily = [
        "# V3_mom2 回放汇总", "",
        f"区间 {summary['start']} 至 {summary['end']}，共 {summary['sessions']} 个交易日。",
        "本报告使用当前部署版回放口径。原附件核对结果见[逐笔对照](../verification/november-source-trades.md)。",
        "",
        "| 信号数 | 已平仓 | 胜率 | 单笔平均净收益 | 合计净盈亏（元） |",
        "| ---: | ---: | ---: | ---: | ---: |",
        table_row(summary["signals"], summary["closed"],
                  number(summary["wins"] / summary["closed"] if summary["closed"] else None, ".2%"),
                  number(summary["mean_net_return"], "+.2%"),
                  number(summary["net_pnl"], "+,.2f")),
        "", "## 每日信号", "",
        "| 信号日 | Top3 | 已平仓 | 已实现净盈亏（元） |",
        "| --- | --- | ---: | ---: |",
    ]
    for report in reports:
        day = report["date"]
        signals = report["signals"]
        closed = [item for item in signals if item["status"] == "closed"]
        names = "、".join(item["name"] for item in signals) or "无信号"
        daily.append(table_row(f"[{day}]({day}.md)", names, len(closed),
                               number(sum(item["net_pnl"] for item in closed) if closed else None, "+,.2f")))
        detail = [
            f"# {day} V3_mom2 盘后信号", "",
            "[回放汇总](summary.md)", "",
            "按 20 日与 60 日动量截面 Z 分之和取前三。次日开盘模拟买入，下一交易日开盘模拟卖出。",
            "",
            "| 排名 | 股票 | 20 日涨幅 | 60 日涨幅 | 得分 | 买入日 / 开盘价 | 卖出日 / 开盘价 | 股数 | 状态 | 净收益 | 净盈亏（元） |",
            "| ---: | --- | ---: | ---: | ---: | --- | --- | ---: | --- | ---: | ---: |",
        ]
        for item in signals:
            detail.append(table_row(
                item["rank"], f"{item['code']} {item['name']}", number(item["ret20"], "+.2%"),
                number(item["ret60"], "+.2%"), number(item["score"], ".3f"),
                f"{item['buy_date']} / {number(item.get('buy_open'), '.2f')}" if item.get("buy_date") else "—",
                f"{item['sell_date']} / {number(item.get('sell_open'), '.2f')}" if item.get("sell_date") else "—",
                item.get("shares", "—"), STATUS.get(item["status"], item["status"]),
                number(item.get("net_return"), "+.2%"), number(item.get("net_pnl"), "+,.2f"),
            ))
        if not signals:
            detail = detail[:6] + ["当日无符合条件的信号。"]
        (directory / f"{day}.md").write_text("\n".join(detail) + "\n", encoding="utf-8")
    (directory / "summary.md").write_text("\n".join(daily) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be on or before --end")
    end = args.end.strftime("%Y%m%d")
    start = (args.start - timedelta(days=240)).strftime("%Y%m%d")
    calendar = fetch(ak.calendar_trade_date_hist_sina)
    sessions = sorted(d for d in pd.to_datetime(calendar["trade_date"]).dt.date
                      if args.start <= d <= args.end)
    if not sessions:
        print("No trading sessions in range")
        return
    pools = {}
    for day in sessions:
        frame = fetch(ak.stock_zt_pool_em, date=day.strftime("%Y%m%d"))
        if frame.empty:
            raise ValueError(f"{day}: empty or unavailable limit-up pool")
        pools[day] = [(str(row["代码"]).zfill(6), str(row["名称"]))
                      for _, row in frame.iterrows()
                      if "ST" not in str(row["名称"]).upper()]
    codes = sorted({code for pool in pools.values() for code, _ in pool})
    with ThreadPoolExecutor(max_workers=3) as executor:
        bars = dict(zip(codes, executor.map(
            lambda code: history(code, start, end, "qfq"), codes)))
    ranked = {day: rank(pool, bars, day) for day, pool in pools.items()}
    picks = sorted({code for frame in ranked.values() for code in frame["code"]})
    with ThreadPoolExecutor(max_workers=3) as executor:
        raw = dict(zip(picks, executor.map(
            lambda code: history(code, start, end, ""), picks)))
    output = []
    for idx, day in enumerate(sessions):
        signals = []
        for pos, signal in enumerate(ranked[day].itertuples(index=False), 1):
            signals.append({"rank": pos, **signal._asdict(),
                            **simulate(signal, raw[signal.code], sessions, idx)})
        output.append({"date": str(day), "strategy": "V3_mom2", "signals": signals})
    directory = Path("reports")
    directory.mkdir(exist_ok=True)
    for report in output:
        path = directory / f"{report['date']}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    closed = [trade for report in output for trade in report["signals"]
              if trade["status"] == "closed"]
    summary = {
        "start": str(args.start), "end": str(args.end), "sessions": len(sessions),
        "signals": sum(len(report["signals"]) for report in output),
        "closed": len(closed), "wins": sum(trade["net_pnl"] > 0 for trade in closed),
        "net_pnl": round(sum(trade["net_pnl"] for trade in closed), 2),
        "mean_net_return": round(sum(trade["net_return"] for trade in closed)
                                 / len(closed), 6) if closed else None,
    }
    body = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    (directory / "summary.json").write_text(body)
    write_markdown(directory, output, summary)
    print(body)


if __name__ == "__main__":
    main()
