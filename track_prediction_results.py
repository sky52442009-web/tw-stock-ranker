from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from tw_stock_ranker import load_config, load_or_fetch_daily


ROOT = Path(__file__).resolve().parent
DEFAULT_RANKING_DIR = ROOT / "outputs"
DEFAULT_HISTORY_FILE = ROOT / "data" / "prediction_log.csv"
DEFAULT_SUMMARY_FILE = ROOT / "data" / "prediction_summary.json"
DEFAULT_CACHE_DIR = ROOT / "data" / "cache"
BUCKETS = [1, 3, 5, 10, 30, 100]

HISTORY_COLUMNS = [
    "prediction_date",
    "planned_target_date",
    "settled_date",
    "status",
    "result",
    "rank",
    "stock_id",
    "name",
    "market",
    "industry_category",
    "signal",
    "long_score",
    "prediction_close",
    "prediction_ret_1d",
    "prediction_turnover",
    "reasons",
    "target_open",
    "target_high",
    "target_low",
    "target_close",
    "open_return",
    "high_return",
    "low_return",
    "close_return",
    "settled_at",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track daily prediction win/loss results.")
    parser.add_argument("--ranking-dir", default=str(DEFAULT_RANKING_DIR), help="Directory with rank CSV files.")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY_FILE), help="Persistent prediction ledger CSV.")
    parser.add_argument("--summary-file", default=str(DEFAULT_SUMMARY_FILE), help="Generated performance summary JSON.")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="Daily market data cache directory.")
    parser.add_argument("--config", default="config.yaml", help="Ranker YAML config.")
    parser.add_argument("--end-date", help="Latest date allowed for settlement, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--max-rank", type=int, default=100, help="How many ranked rows to keep each day.")
    parser.add_argument("--no-network", action="store_true", help="Use cached market data only.")
    return parser.parse_args()


def parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def report_date_from_path(path: Path) -> date | None:
    match = re.search(r"(\d{8})", path.stem)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def next_weekday(value: date) -> date:
    target = value + timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def empty_history() -> pd.DataFrame:
    return pd.DataFrame(columns=HISTORY_COLUMNS)


def read_history(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return empty_history()
    history = pd.read_csv(path, dtype={"stock_id": str})
    for column in HISTORY_COLUMNS:
        if column not in history.columns:
            history[column] = ""
    return history[HISTORY_COLUMNS].copy()


def ranking_files(ranking_dir: Path) -> list[Path]:
    return sorted(ranking_dir.glob("tw_nextday_rank_*.csv"))


def ranking_rows(path: Path, prediction_day: date, max_rank: int) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"stock_id": str})
    if "rank" not in df.columns or "stock_id" not in df.columns:
        return empty_history()
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    df = df[df["rank"].notna() & (df["rank"] <= max_rank)].copy()
    if df.empty:
        return empty_history()

    planned_target = next_weekday(prediction_day).isoformat()
    rows: list[dict[str, Any]] = []
    for _, row in df.sort_values("rank").iterrows():
        rows.append(
            {
                "prediction_date": prediction_day.isoformat(),
                "planned_target_date": planned_target,
                "settled_date": "",
                "status": "pending",
                "result": "pending",
                "rank": int(row.get("rank", 0)),
                "stock_id": str(row.get("stock_id", "")).strip(),
                "name": row.get("name", ""),
                "market": row.get("market", ""),
                "industry_category": row.get("industry_category", ""),
                "signal": row.get("signal", ""),
                "long_score": as_float(row.get("long_score")),
                "prediction_close": as_float(row.get("close")),
                "prediction_ret_1d": as_float(row.get("ret_1d")),
                "prediction_turnover": as_float(row.get("turnover")),
                "reasons": row.get("reasons", ""),
                "target_open": "",
                "target_high": "",
                "target_low": "",
                "target_close": "",
                "open_return": "",
                "high_return": "",
                "low_return": "",
                "close_return": "",
                "settled_at": "",
            }
        )
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)


def ingest_rankings(history: pd.DataFrame, ranking_dir: Path, max_rank: int) -> pd.DataFrame:
    frames = [history.copy()]
    current = history.copy()
    for path in ranking_files(ranking_dir):
        prediction_day = report_date_from_path(path)
        if prediction_day is None:
            continue
        day_key = prediction_day.isoformat()
        existing = current[current["prediction_date"] == day_key]
        has_final_records = bool(existing["status"].isin(["settled", "missing"]).any()) if not existing.empty else False
        if has_final_records:
            continue
        replacement = ranking_rows(path, prediction_day, max_rank)
        if replacement.empty:
            continue
        if not existing.empty:
            current = current[current["prediction_date"] != day_key].copy()
            frames = [current]
        frames.append(replacement)
        current = pd.concat(frames, ignore_index=True)
        frames = [current]
    if not frames:
        return empty_history()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(["prediction_date", "stock_id"], keep="last")
    return out[HISTORY_COLUMNS].sort_values(["prediction_date", "rank", "stock_id"]).reset_index(drop=True)


def find_realized_frame(
    prediction_day: date,
    end_day: date,
    cache_dir: Path,
    cfg: dict[str, Any],
    no_network: bool,
) -> tuple[date | None, pd.DataFrame]:
    candidate = prediction_day + timedelta(days=1)
    while candidate <= end_day:
        if candidate.weekday() < 5:
            frame = load_or_fetch_daily(candidate, cache_dir, cfg, refresh=False, no_network=no_network)
            if not frame.empty:
                return candidate, frame
        candidate += timedelta(days=1)
    return None, pd.DataFrame()


def settle_history(
    history: pd.DataFrame,
    end_day: date,
    cache_dir: Path,
    cfg: dict[str, Any],
    no_network: bool,
) -> pd.DataFrame:
    if history.empty:
        return history
    out = history.copy()
    pending = out[out["status"] == "pending"].copy()
    if pending.empty:
        return out

    settled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for prediction_key, group in pending.groupby("prediction_date", sort=True):
        prediction_day = datetime.strptime(str(prediction_key), "%Y-%m-%d").date()
        target_day, market = find_realized_frame(prediction_day, end_day, cache_dir, cfg, no_network)
        if target_day is None or market.empty:
            continue

        by_stock = market.drop_duplicates("stock_id", keep="last").set_index("stock_id")
        for index, row in group.iterrows():
            stock_id = str(row["stock_id"])
            prediction_close = as_float(row.get("prediction_close"))
            if stock_id not in by_stock.index or not prediction_close or prediction_close <= 0:
                out.loc[index, ["status", "result", "settled_date", "settled_at"]] = [
                    "missing",
                    "missing",
                    target_day.isoformat(),
                    settled_at,
                ]
                continue

            realized = by_stock.loc[stock_id]
            target_open = as_float(realized.get("open"))
            target_high = as_float(realized.get("high"))
            target_low = as_float(realized.get("low"))
            target_close = as_float(realized.get("close"))
            if target_close is None:
                out.loc[index, ["status", "result", "settled_date", "settled_at"]] = [
                    "missing",
                    "missing",
                    target_day.isoformat(),
                    settled_at,
                ]
                continue

            close_return = target_close / prediction_close - 1
            result = "win" if close_return > 0 else "loss" if close_return < 0 else "tie"
            out.loc[index, "settled_date"] = target_day.isoformat()
            out.loc[index, "status"] = "settled"
            out.loc[index, "result"] = result
            out.loc[index, "target_open"] = target_open if target_open is not None else ""
            out.loc[index, "target_high"] = target_high if target_high is not None else ""
            out.loc[index, "target_low"] = target_low if target_low is not None else ""
            out.loc[index, "target_close"] = target_close
            out.loc[index, "open_return"] = "" if target_open is None else target_open / prediction_close - 1
            out.loc[index, "high_return"] = "" if target_high is None else target_high / prediction_close - 1
            out.loc[index, "low_return"] = "" if target_low is None else target_low / prediction_close - 1
            out.loc[index, "close_return"] = close_return
            out.loc[index, "settled_at"] = settled_at

    return out[HISTORY_COLUMNS].sort_values(["prediction_date", "rank", "stock_id"]).reset_index(drop=True)


def pct(value: float | None) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 6)


def bucket_stats(history: pd.DataFrame, rank_limit: int) -> dict[str, Any]:
    ranked = history[pd.to_numeric(history["rank"], errors="coerce") <= rank_limit].copy()
    evaluated = ranked[ranked["result"].isin(["win", "loss", "tie"])].copy()
    if evaluated.empty:
        return {
            "rank_limit": rank_limit,
            "settled": 0,
            "pending": int((ranked["status"] == "pending").sum()),
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "win_rate": None,
            "avg_close_return": None,
            "best_close_return": None,
            "worst_close_return": None,
        }
    close_returns = pd.to_numeric(evaluated["close_return"], errors="coerce")
    wins = int((evaluated["result"] == "win").sum())
    losses = int((evaluated["result"] == "loss").sum())
    ties = int((evaluated["result"] == "tie").sum())
    denominator = wins + losses + ties
    return {
        "rank_limit": rank_limit,
        "settled": int(len(evaluated)),
        "pending": int((ranked["status"] == "pending").sum()),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": pct(wins / denominator) if denominator else None,
        "avg_close_return": pct(close_returns.mean()),
        "best_close_return": pct(close_returns.max()),
        "worst_close_return": pct(close_returns.min()),
    }


def result_label(value: Any) -> str:
    labels = {"win": "win", "loss": "loss", "tie": "tie", "pending": "pending", "missing": "missing"}
    return labels.get(str(value), str(value))


def recent_daily_rows(history: pd.DataFrame, limit: int = 20) -> list[dict[str, Any]]:
    evaluated = history[history["result"].isin(["win", "loss", "tie"])].copy()
    if evaluated.empty:
        return []
    evaluated["rank_num"] = pd.to_numeric(evaluated["rank"], errors="coerce")
    evaluated["close_return_num"] = pd.to_numeric(evaluated["close_return"], errors="coerce")

    rows: list[dict[str, Any]] = []
    for prediction_date, group in evaluated.groupby("prediction_date", sort=True):
        group = group.sort_values("rank_num")
        top1 = group[group["rank_num"] <= 1]
        top3 = group[group["rank_num"] <= 3]
        top10 = group[group["rank_num"] <= 10]
        wins10 = int((top10["result"] == "win").sum())
        total10 = int(len(top10))
        rows.append(
            {
                "prediction_date": prediction_date,
                "settled_date": str(group["settled_date"].dropna().iloc[0]) if not group["settled_date"].dropna().empty else "",
                "top1_stock": "" if top1.empty else f"{top1.iloc[0]['stock_id']} {top1.iloc[0]['name']}",
                "top1_result": "" if top1.empty else result_label(top1.iloc[0]["result"]),
                "top1_return": None if top1.empty else pct(top1.iloc[0]["close_return_num"]),
                "top3_win_rate": None if top3.empty else pct((top3["result"] == "win").sum() / len(top3)),
                "top10_win_rate": None if not total10 else pct(wins10 / total10),
                "top10_avg_return": None if top10.empty else pct(top10["close_return_num"].mean()),
                "top10_settled": total10,
            }
        )
    return rows[-limit:][::-1]


def write_summary(history: pd.DataFrame, path: Path, max_rank: int) -> dict[str, Any]:
    summary = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "max_rank": max_rank,
        "rows": int(len(history)),
        "pending": int((history["status"] == "pending").sum()) if not history.empty else 0,
        "missing": int((history["status"] == "missing").sum()) if not history.empty else 0,
        "buckets": {f"top_{bucket}": bucket_stats(history, bucket) for bucket in BUCKETS},
        "recent_daily": recent_daily_rows(history),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def write_history(history: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(path, index=False, encoding="utf-8-sig")


def main() -> int:
    args = parse_args()
    ranking_dir = Path(args.ranking_dir)
    history_file = Path(args.history_file)
    summary_file = Path(args.summary_file)
    cache_dir = Path(args.cache_dir)
    cfg = load_config(args.config)
    end_day = parse_date(args.end_date)

    history = read_history(history_file)
    history = ingest_rankings(history, ranking_dir, args.max_rank)
    history = settle_history(history, end_day, cache_dir, cfg, args.no_network)
    write_history(history, history_file)
    summary = write_summary(history, summary_file, args.max_rank)

    top10 = summary["buckets"]["top_10"]
    print(f"Prediction rows: {summary['rows']}")
    print(f"Pending rows: {summary['pending']}")
    print(
        "Top10 settled/wins/losses/win_rate: "
        f"{top10['settled']}/{top10['wins']}/{top10['losses']}/{top10['win_rate']}"
    )
    print(f"History: {history_file}")
    print(f"Summary: {summary_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
