from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "lookback_calendar_days": 130,
    "top_n": 30,
    "min_history_days": 45,
    "min_20d_avg_turnover": 200_000_000,
    "http_timeout_seconds": 30,
    "request_sleep_seconds": 0.12,
    "skip_weekends": True,
    "require_complete_market_data": True,
    "expected_markets": ["twse", "tpex"],
    "min_rows_by_market": {
        "twse": 900,
        "tpex": 700,
    },
    "favored_industries": [
        "半導體業",
        "電子零組件業",
        "電腦及週邊設備業",
        "其他電子業",
        "通信網路業",
        "光電業",
    ],
    "weights": {
        "liquidity": 14,
        "volume_breakout": 16,
        "relative_strength": 12,
        "close_strength": 12,
        "trend_quality": 13,
        "macd": 9,
        "rsi": 8,
        "breakout_after_base": 9,
        "industry": 7,
    },
    "risk_penalties": {
        "near_limit_up": 9,
        "long_upper_shadow": 8,
        "very_hot_rsi": 6,
        "close_below_vwap": 5,
        "low_liquidity": 14,
        "extreme_gap": 4,
        "optional_daytrade_crowded": 8,
        "optional_margin_hot": 6,
        "optional_attention": 100,
    },
}


TWSE_DAILY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_DAILY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
FINMIND_STOCK_INFO_URL = "https://api.finmindtrade.com/api/v4/data"


@dataclass(frozen=True)
class FetchResult:
    frame: pd.DataFrame
    source_dates: list[str]


class DataFetchError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="每日台股隔日上漲候選排行器，依研究報告中的事件、量能、趨勢與風險條件評分。",
    )
    parser.add_argument("--config", default="config.yaml", help="YAML 設定檔路徑。")
    parser.add_argument("--end-date", help="資料截止日，格式 YYYY-MM-DD。預設為今天。")
    parser.add_argument("--lookback-days", type=int, help="往前抓取的日曆天數。")
    parser.add_argument("--top", type=int, help="輸出前 N 名。")
    parser.add_argument("--cache-dir", default="data/cache", help="日資料快取資料夾。")
    parser.add_argument("--output-dir", default="outputs", help="排行報表輸出資料夾。")
    parser.add_argument("--refresh", action="store_true", help="忽略既有日資料快取重新下載。")
    parser.add_argument(
        "--daytrade-file",
        help="可選 CSV：date,stock_id,daytrade_ratio。若提供，會對高當沖擁擠度做風險扣分。",
    )
    parser.add_argument(
        "--margin-file",
        help="可選 CSV：date,stock_id,margin_buy_ratio 或 margin_balance_change_ratio。",
    )
    parser.add_argument(
        "--attention-file",
        help="可選 CSV：date,stock_id,attention_flag。可放注意/處置/警示名單，flag 為 1 會排除。",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="只使用快取資料，不連線下載。",
    )
    return parser.parse_args()


def load_config(path: str | Path) -> dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    cfg["weights"] = DEFAULT_CONFIG["weights"].copy()
    cfg["risk_penalties"] = DEFAULT_CONFIG["risk_penalties"].copy()

    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
        deep_update(cfg, user_cfg)
    return cfg


def deep_update(base: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value


def parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def clean_number(value: Any) -> float:
    if value is None:
        return np.nan
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    text = str(value).strip()
    if text in {"", "--", "---", "除權息", "X", "NaN"}:
        return np.nan
    text = re.sub(r"<[^>]*>", "", text)
    text = text.replace(",", "").replace("+", "").replace("\u00a0", "")
    text = text.replace(" ", "")
    if text in {"", "--", "---"}:
        return np.nan
    try:
        return float(text)
    except ValueError:
        return np.nan


def is_common_stock_id(stock_id: Any) -> bool:
    text = str(stock_id).strip()
    return bool(re.fullmatch(r"\d{4}", text))


def date_range(start: date, end: date) -> list[date]:
    days = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def request_json(url: str, params: dict[str, Any], timeout: int) -> Any:
    headers = {
        "User-Agent": "tw-nextday-ranker/1.0 (+local research tool)",
        "Accept": "application/json,text/plain,*/*",
    }
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    text = response.content.decode("utf-8-sig", errors="replace")
    return json.loads(text)


def fetch_twse_daily(day: date, cfg: dict[str, Any]) -> pd.DataFrame:
    payload = request_json(
        TWSE_DAILY_URL,
        {
            "date": day.strftime("%Y%m%d"),
            "type": "ALLBUT0999",
            "response": "json",
        },
        int(cfg["http_timeout_seconds"]),
    )
    tables = payload.get("tables") or []
    target = None
    for table in tables:
        fields = table.get("fields") or []
        if "證券代號" in fields and "收盤價" in fields and "成交金額" in fields:
            target = table
            break
    if target is None:
        return empty_daily_frame()

    rows: list[dict[str, Any]] = []
    fields = target["fields"]
    for raw in target.get("data") or []:
        item = dict(zip(fields, raw, strict=False))
        stock_id = str(item.get("證券代號", "")).strip()
        if not is_common_stock_id(stock_id):
            continue
        rows.append(
            {
                "date": pd.Timestamp(day),
                "stock_id": stock_id,
                "name": str(item.get("證券名稱", "")).strip(),
                "market": "twse",
                "open": clean_number(item.get("開盤價")),
                "high": clean_number(item.get("最高價")),
                "low": clean_number(item.get("最低價")),
                "close": clean_number(item.get("收盤價")),
                "volume": clean_number(item.get("成交股數")),
                "turnover": clean_number(item.get("成交金額")),
                "transactions": clean_number(item.get("成交筆數")),
            }
        )
    return normalize_daily_frame(pd.DataFrame(rows))


def fetch_tpex_daily(day: date, cfg: dict[str, Any]) -> pd.DataFrame:
    payload = request_json(
        TPEX_DAILY_URL,
        {
            "date": day.strftime("%Y/%m/%d"),
            "response": "json",
        },
        int(cfg["http_timeout_seconds"]),
    )
    tables = payload.get("tables") or []
    target = None
    for table in tables:
        fields = table.get("fields") or []
        if "代號" in fields and "收盤" in fields and "成交金額(元)" in fields:
            target = table
            break
    if target is None:
        return empty_daily_frame()

    rows: list[dict[str, Any]] = []
    fields = target["fields"]
    for raw in target.get("data") or []:
        item = dict(zip(fields, raw, strict=False))
        stock_id = str(item.get("代號", "")).strip()
        if not is_common_stock_id(stock_id):
            continue
        rows.append(
            {
                "date": pd.Timestamp(day),
                "stock_id": stock_id,
                "name": str(item.get("名稱", "")).strip(),
                "market": "tpex",
                "open": clean_number(item.get("開盤")),
                "high": clean_number(item.get("最高")),
                "low": clean_number(item.get("最低")),
                "close": clean_number(item.get("收盤")),
                "volume": clean_number(item.get("成交股數")),
                "turnover": clean_number(item.get("成交金額(元)")),
                "transactions": clean_number(item.get("成交筆數")),
            }
        )
    return normalize_daily_frame(pd.DataFrame(rows))


def empty_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "stock_id",
            "name",
            "market",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover",
            "transactions",
        ]
    )


def normalize_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return empty_daily_frame()
    numeric_cols = ["open", "high", "low", "close", "volume", "turnover", "transactions"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume", "turnover"])
    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0)]
    return df


def cache_path(cache_dir: Path, day: date) -> Path:
    return cache_dir / f"tw_daily_{day.strftime('%Y%m%d')}.csv"


def market_quality_issues(df: pd.DataFrame, cfg: dict[str, Any]) -> list[str]:
    if df.empty:
        return ["empty"]
    expected = set(cfg.get("expected_markets", ["twse", "tpex"]))
    present = set(df["market"].dropna().astype(str).unique())
    issues = [f"missing {market}" for market in sorted(expected - present)]

    min_rows = cfg.get("min_rows_by_market", {}) or {}
    counts = df.groupby("market").size().to_dict()
    for market, minimum in min_rows.items():
        count = int(counts.get(market, 0))
        if count and count < int(minimum):
            issues.append(f"{market} rows {count} < {minimum}")
    return issues


def has_complete_market_data(df: pd.DataFrame, cfg: dict[str, Any]) -> bool:
    if not bool(cfg.get("require_complete_market_data", True)):
        return True
    return not market_quality_issues(df, cfg)


def load_or_fetch_daily(day: date, cache_dir: Path, cfg: dict[str, Any], refresh: bool, no_network: bool) -> pd.DataFrame:
    path = cache_path(cache_dir, day)
    had_invalid_cache = False
    if path.exists() and not refresh:
        cached = pd.read_csv(path, dtype={"stock_id": str}, parse_dates=["date"])
        if has_complete_market_data(cached, cfg):
            return cached
        had_invalid_cache = True
        issues = "; ".join(market_quality_issues(cached, cfg))
        if no_network:
            print(f"[warn] {day} 快取資料不完整，已略過：{issues}", file=sys.stderr)
            return empty_daily_frame()
        print(f"[warn] {day} 快取資料不完整，重新下載：{issues}", file=sys.stderr)
    if no_network:
        return empty_daily_frame()

    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for fetcher, label in [(fetch_twse_daily, "TWSE"), (fetch_tpex_daily, "TPEx")]:
        try:
            frame = fetcher(day, cfg)
            if not frame.empty:
                frames.append(frame)
        except Exception as exc:  # noqa: BLE001 - keep daily run resilient.
            errors.append(f"{label}: {exc}")

    if not frames:
        if errors and day.weekday() < 5:
            print(f"[warn] {day} 沒有抓到日資料：{'; '.join(errors)}", file=sys.stderr)
        return empty_daily_frame()

    df = pd.concat(frames, ignore_index=True)
    if not has_complete_market_data(df, cfg):
        issues = "; ".join(market_quality_issues(df, cfg))
        if day.weekday() < 5:
            print(f"[warn] {day} 下載資料不完整，已略過且不寫入快取：{issues}", file=sys.stderr)
        if had_invalid_cache and path.exists():
            path.unlink()
        return empty_daily_frame()
    cache_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    time.sleep(float(cfg["request_sleep_seconds"]))
    return df


def load_price_history(
    end_day: date,
    lookback_days: int,
    cache_dir: Path,
    cfg: dict[str, Any],
    refresh: bool,
    no_network: bool,
) -> FetchResult:
    start_day = end_day - timedelta(days=lookback_days)
    frames = []
    source_dates = []
    for day in date_range(start_day, end_day):
        if bool(cfg.get("skip_weekends", True)) and day.weekday() >= 5:
            continue
        frame = load_or_fetch_daily(day, cache_dir, cfg, refresh, no_network)
        if not frame.empty:
            frames.append(frame)
            source_dates.append(day.strftime("%Y-%m-%d"))
    if not frames:
        raise DataFetchError("沒有可用日資料。請確認網路連線、日期，或先建立快取。")
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(["date", "stock_id"], keep="last")
    return FetchResult(df.sort_values(["stock_id", "date"]).reset_index(drop=True), source_dates)


def load_stock_info(cache_dir: Path, cfg: dict[str, Any], no_network: bool, refresh: bool) -> pd.DataFrame:
    path = cache_dir / "stock_info_finmind.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype={"stock_id": str})
    if no_network:
        return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category", "type"])

    payload = request_json(
        FINMIND_STOCK_INFO_URL,
        {"dataset": "TaiwanStockInfo"},
        int(cfg["http_timeout_seconds"]),
    )
    data = payload.get("data") or []
    if not data:
        return pd.DataFrame(columns=["stock_id", "stock_name", "industry_category", "type"])
    info = pd.DataFrame(data)
    info["stock_id"] = info["stock_id"].astype(str)
    info = info[info["stock_id"].map(is_common_stock_id)].copy()
    if "date" in info.columns:
        info = info.sort_values("date").drop_duplicates("stock_id", keep="last")
    keep = [c for c in ["stock_id", "stock_name", "industry_category", "type", "date"] if c in info.columns]
    info = info[keep].copy()
    cache_dir.mkdir(parents=True, exist_ok=True)
    info.to_csv(path, index=False, encoding="utf-8-sig")
    return info


def add_optional_overlay(base: pd.DataFrame, file_path: str | None, label: str) -> pd.DataFrame:
    if not file_path:
        return base
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"{label} 檔案不存在：{path}")
    overlay = pd.read_csv(path, dtype={"stock_id": str})
    required = {"date", "stock_id"}
    missing = required - set(overlay.columns)
    if missing:
        raise ValueError(f"{label} 缺少欄位：{', '.join(sorted(missing))}")
    overlay["date"] = pd.to_datetime(overlay["date"])
    return base.merge(overlay, on=["date", "stock_id"], how="left")


def ema_by_group(df: pd.DataFrame, column: str, span: int) -> pd.Series:
    return (
        df.groupby("stock_id", group_keys=False)[column]
        .apply(lambda s: s.ewm(span=span, adjust=False, min_periods=span).mean())
        .reindex(df.index)
    )


def rolling_by_group(df: pd.DataFrame, column: str, window: int, fn: str, min_periods: int | None = None) -> pd.Series:
    minp = min_periods if min_periods is not None else window
    rolled = df.groupby("stock_id")[column].rolling(window, min_periods=minp)
    if fn == "mean":
        out = rolled.mean()
    elif fn == "std":
        out = rolled.std()
    elif fn == "max":
        out = rolled.max()
    elif fn == "min":
        out = rolled.min()
    else:
        raise ValueError(f"unsupported rolling fn: {fn}")
    return out.reset_index(level=0, drop=True).reindex(df.index)


def add_features(df: pd.DataFrame, info: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    df = df.sort_values(["stock_id", "date"]).copy()
    g = df.groupby("stock_id", group_keys=False)

    df["prev_close"] = g["close"].shift(1)
    df["ret_1d"] = df["close"] / df["prev_close"] - 1
    df["gap_pct"] = df["open"] / df["prev_close"] - 1
    df["range_pct"] = (df["high"] - df["low"]) / df["prev_close"]
    day_range = (df["high"] - df["low"]).replace(0, np.nan)
    df["intraday_pos"] = ((df["close"] - df["low"]) / day_range).clip(0, 1).fillna(0.5)
    df["upper_shadow_ratio"] = ((df["high"] - df[["open", "close"]].max(axis=1)) / day_range).clip(0, 1).fillna(0)
    df["lower_shadow_ratio"] = ((df[["open", "close"]].min(axis=1) - df["low"]) / day_range).clip(0, 1).fillna(0)
    df["body_ratio"] = ((df["close"] - df["open"]).abs() / day_range).clip(0, 1).fillna(0)
    df["vwap_proxy"] = np.where(df["volume"] > 0, df["turnover"] / df["volume"], np.nan)
    df["close_vwap_diff"] = df["close"] / df["vwap_proxy"] - 1

    for window in [5, 10, 20, 60]:
        df[f"ma{window}"] = rolling_by_group(df, "close", window, "mean")
    for window in [5, 20]:
        df[f"vol_ma{window}"] = rolling_by_group(df, "volume", window, "mean")
        df[f"money_ma{window}"] = rolling_by_group(df, "turnover", window, "mean")

    df["prev_high_20"] = rolling_by_group(df, "high", 20, "max").groupby(df["stock_id"]).shift(1)
    df["prev_low_20"] = rolling_by_group(df, "low", 20, "min").groupby(df["stock_id"]).shift(1)
    df["prev_vol_max_5"] = rolling_by_group(df, "volume", 5, "max").groupby(df["stock_id"]).shift(1)
    df["vol_ratio_5"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)
    df["vol_break_5d"] = df["volume"] > df["prev_vol_max_5"]
    df["breakout_20d"] = df["close"] >= df["prev_high_20"] * 0.995
    df["prior_base_width_20"] = (df["prev_high_20"] - df["prev_low_20"]) / df["prev_low_20"].replace(0, np.nan)
    df["base_quality"] = (1 - (df["prior_base_width_20"] / 0.22)).clip(0, 1)

    df["ma20_slope_5d"] = df["ma20"] / g["ma20"].shift(5) - 1
    df["above_ma20"] = df["close"] > df["ma20"]
    df["above_ma60"] = df["close"] > df["ma60"]

    delta = g["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.groupby(df["stock_id"]).rolling(14, min_periods=14).mean().reset_index(level=0, drop=True)
    avg_loss = losses.groupby(df["stock_id"]).rolling(14, min_periods=14).mean().reset_index(level=0, drop=True)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))
    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi14"] = 100
    df.loc[(avg_loss == 0) & (avg_gain == 0), "rsi14"] = 50

    ema12 = ema_by_group(df, "close", 12)
    ema26 = ema_by_group(df, "close", 26)
    df["macd_dif"] = ema12 - ema26
    df["macd_signal"] = (
        df.groupby("stock_id", group_keys=False)["macd_dif"]
        .apply(lambda s: s.ewm(span=9, adjust=False, min_periods=9).mean())
        .reindex(df.index)
    )
    df["macd_hist"] = df["macd_dif"] - df["macd_signal"]
    df["macd_hist_prev"] = g["macd_hist"].shift(1)
    df["macd_hist_expanding"] = (df["macd_hist"] > 0) & (df["macd_hist"] > df["macd_hist_prev"])

    df["bb_mid"] = df["ma20"]
    df["bb_std"] = rolling_by_group(df, "close", 20, "std")
    df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    df["bb_pct_b"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

    high_low = df["high"] - df["low"]
    high_prev = (df["high"] - df["prev_close"]).abs()
    low_prev = (df["low"] - df["prev_close"]).abs()
    df["true_range"] = pd.concat([high_low, high_prev, low_prev], axis=1).max(axis=1)
    df["atr14"] = df.groupby("stock_id")["true_range"].rolling(14, min_periods=14).mean().reset_index(level=0, drop=True)
    df["atr14_pct"] = df["atr14"] / df["close"]

    df["history_days"] = g.cumcount() + 1
    df["money_rank_pct"] = df.groupby("date")["turnover"].rank(pct=True)
    df["ret_rank_pct"] = df.groupby("date")["ret_1d"].rank(pct=True)
    df["range_rank_pct"] = df.groupby("date")["range_pct"].rank(pct=True)

    if not info.empty:
        info = info.rename(columns={"stock_name": "info_name"})
        df = df.merge(info[["stock_id", "info_name", "industry_category"]], on="stock_id", how="left")
        df["name"] = df["name"].where(df["name"].notna() & (df["name"] != ""), df.get("info_name"))
        df = df.drop(columns=[c for c in ["info_name"] if c in df.columns])
    else:
        df["industry_category"] = np.nan

    industry_heat = (
        df.groupby(["date", "industry_category"], dropna=False)["ret_1d"]
        .median()
        .rename("industry_median_ret")
        .reset_index()
    )
    industry_heat["industry_heat_rank"] = industry_heat.groupby("date")["industry_median_ret"].rank(pct=True)
    df = df.merge(industry_heat, on=["date", "industry_category"], how="left")

    return df


def score_piece(value: pd.Series, low: float, high: float) -> pd.Series:
    return ((value - low) / (high - low)).clip(0, 1).fillna(0)


def score_rsi(rsi: pd.Series) -> pd.Series:
    # Best zone for a next-day long candidate: strong but not already overheated.
    score = pd.Series(0.0, index=rsi.index)
    score = score.where(~((rsi >= 45) & (rsi <= 68)), 1.0)
    score = score.where(~((rsi > 68) & (rsi <= 76)), 0.65)
    score = score.where(~((rsi >= 38) & (rsi < 45)), 0.55)
    score = score.where(~((rsi > 76) & (rsi <= 82)), 0.3)
    return score.fillna(0)


def compute_scores(df: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    weights = cfg["weights"]
    penalties = cfg["risk_penalties"]
    favored_industries = set(cfg.get("favored_industries", []))

    out = df.copy()
    out["liquidity_score"] = score_piece(out["money_ma20"], cfg["min_20d_avg_turnover"], cfg["min_20d_avg_turnover"] * 5)
    out["volume_breakout_score"] = np.select(
        [
            out["vol_break_5d"],
            out["vol_ratio_5"] >= 1.8,
            out["vol_ratio_5"] >= 1.25,
        ],
        [1.0, 0.75, 0.45],
        default=0.0,
    )
    out["relative_strength_score"] = out["ret_rank_pct"].fillna(0).clip(0, 1)
    out["close_strength_score"] = out["intraday_pos"].fillna(0.5).clip(0, 1)
    out["trend_quality_score"] = (
        0.35 * out["above_ma20"].fillna(False).astype(float)
        + 0.25 * out["above_ma60"].fillna(False).astype(float)
        + 0.40 * score_piece(out["ma20_slope_5d"], 0, 0.025)
    ).clip(0, 1)
    out["macd_score"] = out["macd_hist_expanding"].fillna(False).astype(float)
    out["rsi_score"] = score_rsi(out["rsi14"])
    out["breakout_after_base_score"] = (
        out["breakout_20d"].fillna(False).astype(float) * (0.45 + 0.55 * out["base_quality"].fillna(0))
    ).clip(0, 1)
    out["industry_score"] = (
        0.55 * out["industry_heat_rank"].fillna(0)
        + 0.45 * out["industry_category"].isin(favored_industries).astype(float)
    ).clip(0, 1)

    out["raw_long_score"] = (
        weights["liquidity"] * out["liquidity_score"]
        + weights["volume_breakout"] * out["volume_breakout_score"]
        + weights["relative_strength"] * out["relative_strength_score"]
        + weights["close_strength"] * out["close_strength_score"]
        + weights["trend_quality"] * out["trend_quality_score"]
        + weights["macd"] * out["macd_score"]
        + weights["rsi"] * out["rsi_score"]
        + weights["breakout_after_base"] * out["breakout_after_base_score"]
        + weights["industry"] * out["industry_score"]
    )

    out["near_limit_up"] = out["ret_1d"] >= 0.09
    out["long_upper_shadow"] = (out["upper_shadow_ratio"] >= 0.42) & (out["intraday_pos"] <= 0.68)
    out["very_hot_rsi"] = out["rsi14"] >= 82
    out["close_below_vwap"] = out["close_vwap_diff"] < -0.004
    out["low_liquidity"] = out["money_ma20"] < cfg["min_20d_avg_turnover"]
    out["extreme_gap"] = out["gap_pct"].abs() >= 0.055

    out["risk_penalty"] = 0.0
    for col, penalty in [
        ("near_limit_up", "near_limit_up"),
        ("long_upper_shadow", "long_upper_shadow"),
        ("very_hot_rsi", "very_hot_rsi"),
        ("close_below_vwap", "close_below_vwap"),
        ("low_liquidity", "low_liquidity"),
        ("extreme_gap", "extreme_gap"),
    ]:
        out["risk_penalty"] += out[col].fillna(False).astype(float) * float(penalties[penalty])

    if "daytrade_ratio" in out.columns:
        out["daytrade_crowded"] = out["daytrade_ratio"].fillna(0) >= 0.50
        out["risk_penalty"] += out["daytrade_crowded"].astype(float) * float(penalties["optional_daytrade_crowded"])
    else:
        out["daytrade_crowded"] = False

    if "margin_buy_ratio" in out.columns:
        out["margin_hot"] = out["margin_buy_ratio"].fillna(0) >= 0.18
        out["risk_penalty"] += out["margin_hot"].astype(float) * float(penalties["optional_margin_hot"])
    elif "margin_balance_change_ratio" in out.columns:
        out["margin_hot"] = out["margin_balance_change_ratio"].fillna(0) >= 0.12
        out["risk_penalty"] += out["margin_hot"].astype(float) * float(penalties["optional_margin_hot"])
    else:
        out["margin_hot"] = False

    if "attention_flag" in out.columns:
        out["attention_blocked"] = out["attention_flag"].fillna(0).astype(float) >= 1
        out["risk_penalty"] += out["attention_blocked"].astype(float) * float(penalties["optional_attention"])
    else:
        out["attention_blocked"] = False

    out["long_score"] = (out["raw_long_score"] - out["risk_penalty"]).clip(0, 100)
    out["rank"] = out["long_score"].rank(ascending=False, method="first").astype(int)
    out["signal"] = np.select(
        [
            out["attention_blocked"],
            out["long_score"] >= 72,
            out["long_score"] >= 58,
            out["long_score"] >= 45,
        ],
        ["排除", "偏多", "觀察偏多", "中性觀察"],
        default="低優先",
    )
    out["reasons"] = out.apply(build_reason_text, axis=1)
    return out


def build_reason_text(row: pd.Series) -> str:
    reasons: list[str] = []
    if row.get("vol_break_5d"):
        reasons.append("量破5日高")
    elif row.get("vol_ratio_5", 0) >= 1.8:
        reasons.append("量比>1.8")
    if row.get("breakout_20d"):
        reasons.append("接近20日突破")
    if row.get("intraday_pos", 0) >= 0.78:
        reasons.append("收在高檔")
    if row.get("macd_hist_expanding"):
        reasons.append("MACD動能擴大")
    rsi = row.get("rsi14")
    if pd.notna(rsi):
        if 45 <= rsi <= 68:
            reasons.append("RSI健康")
        elif rsi >= 82:
            reasons.append("RSI過熱扣分")
    if row.get("industry_score", 0) >= 0.75:
        reasons.append("產業相對強")
    if row.get("near_limit_up"):
        reasons.append("近漲停過熱扣分")
    if row.get("long_upper_shadow"):
        reasons.append("長上影扣分")
    if row.get("close_below_vwap"):
        reasons.append("收低於VWAP扣分")
    if row.get("low_liquidity"):
        reasons.append("流動性不足扣分")
    if row.get("attention_blocked"):
        reasons.append("注意/處置排除")
    return "、".join(reasons[:8])


def latest_trading_frame(df: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    latest_date = df["date"].max()
    latest = df[df["date"] == latest_date].copy()
    latest = latest[latest["history_days"] >= int(cfg["min_history_days"])]
    latest = latest[latest["close"].notna() & latest["ret_1d"].notna()]
    return latest


def format_percent(value: Any, digits: int = 2) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value) * 100:.{digits}f}%"


def format_number(value: Any, digits: int = 2) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value):,.{digits}f}"


def markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_沒有符合條件的資料_"
    rendered = df.astype(str).replace({"nan": "", "None": ""})

    def esc(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    headers = [esc(c) for c in rendered.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for _, row in rendered.iterrows():
        lines.append("| " + " | ".join(esc(row[c]) for c in rendered.columns) + " |")
    return "\n".join(lines)


def report_columns() -> list[str]:
    return [
        "rank",
        "stock_id",
        "name",
        "market",
        "industry_category",
        "signal",
        "long_score",
        "raw_long_score",
        "risk_penalty",
        "close",
        "ret_1d",
        "turnover",
        "money_ma20",
        "vol_ratio_5",
        "intraday_pos",
        "rsi14",
        "macd_hist",
        "close_vwap_diff",
        "breakout_20d",
        "near_limit_up",
        "long_upper_shadow",
        "reasons",
    ]


def write_outputs(ranked: pd.DataFrame, output_dir: Path, latest_date: pd.Timestamp, top_n: int) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = latest_date.strftime("%Y%m%d")
    csv_path = output_dir / f"tw_nextday_rank_{stamp}.csv"
    md_path = output_dir / f"tw_nextday_rank_{stamp}.md"

    columns = [c for c in report_columns() if c in ranked.columns]
    export = ranked[columns].sort_values("rank").copy()
    export.to_csv(csv_path, index=False, encoding="utf-8-sig")

    top = export.head(top_n).copy()
    display = top.copy()
    for col in ["ret_1d", "close_vwap_diff", "intraday_pos"]:
        if col in display.columns:
            display[col] = display[col].map(format_percent)
    for col in ["turnover", "money_ma20"]:
        if col in display.columns:
            display[col] = display[col].map(lambda v: format_number(v, 0))
    for col in ["long_score", "raw_long_score", "risk_penalty", "rsi14", "macd_hist", "vol_ratio_5", "close"]:
        if col in display.columns:
            display[col] = display[col].map(format_number)

    md = [
        f"# 台股隔日上漲候選排行（{latest_date.strftime('%Y-%m-%d')} 盤後）",
        "",
        "> 這是量化研究工具輸出的候選排行，不是投資建議或保證預測。實盤前仍需確認重大訊息、注意/處置、可當沖資格、券源、流動性與風險上限。",
        "",
        f"共輸出 {len(export)} 檔，以下顯示前 {min(top_n, len(top))} 檔。",
        "",
        markdown_table(display),
        "",
        "## 評分邏輯摘要",
        "",
        "多頭分數偏重：20 日均成交值、爆量、相對強度、收盤位置、均線趨勢、MACD、RSI、盤整後突破與產業熱度。",
        "",
        "風險扣分包含：近漲停過熱、長上影、RSI 過熱、收低於 VWAP、低流動性、極端跳空，以及可選匯入的當沖擁擠、融資過熱、注意/處置名單。",
        "",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    return csv_path, md_path


def print_console_summary(ranked: pd.DataFrame, latest_date: pd.Timestamp, top_n: int) -> None:
    cols = ["rank", "stock_id", "name", "signal", "long_score", "close", "ret_1d", "reasons"]
    top = ranked.sort_values("rank").head(top_n)[cols].copy()
    top["long_score"] = top["long_score"].map(lambda v: f"{v:.2f}")
    top["ret_1d"] = top["ret_1d"].map(lambda v: format_percent(v))
    print(f"\n台股隔日上漲候選排行：{latest_date.strftime('%Y-%m-%d')} 盤後")
    print(top.to_string(index=False))


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)
    if args.lookback_days:
        cfg["lookback_calendar_days"] = args.lookback_days
    if args.top:
        cfg["top_n"] = args.top

    end_day = parse_date(args.end_date)
    cache_dir = Path(args.cache_dir)
    output_dir = Path(args.output_dir)

    try:
        fetch_result = load_price_history(
            end_day=end_day,
            lookback_days=int(cfg["lookback_calendar_days"]),
            cache_dir=cache_dir,
            cfg=cfg,
            refresh=args.refresh,
            no_network=args.no_network,
        )
        info = load_stock_info(cache_dir, cfg, args.no_network, args.refresh)
        df = add_optional_overlay(fetch_result.frame, args.daytrade_file, "daytrade")
        df = add_optional_overlay(df, args.margin_file, "margin")
        df = add_optional_overlay(df, args.attention_file, "attention")
        featured = add_features(df, info, cfg)
        latest = latest_trading_frame(featured, cfg)
        if latest.empty:
            raise DataFetchError("最新交易日資料不足，無法產生排行；請增加 lookback-days 或降低 min_history_days。")
        scored = compute_scores(latest, cfg)
        ranked = scored.sort_values(["long_score", "turnover"], ascending=[False, False]).copy()
        ranked["rank"] = range(1, len(ranked) + 1)
        csv_path, md_path = write_outputs(
            ranked=ranked,
            output_dir=output_dir,
            latest_date=ranked["date"].max(),
            top_n=int(cfg["top_n"]),
        )
        print_console_summary(ranked, ranked["date"].max(), int(cfg["top_n"]))
        print(f"\nCSV: {csv_path}")
        print(f"Markdown: {md_path}")
        print(f"資料日期範圍：{fetch_result.source_dates[0]} ~ {fetch_result.source_dates[-1]}")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should return clear error.
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
