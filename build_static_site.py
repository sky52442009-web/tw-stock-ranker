from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from serve_ranker import find_latest_csv, html_page, load_rows, report_date_from_path


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
SITE = ROOT / "site"
DATA = ROOT / "data"
PREDICTION_LOG = DATA / "prediction_log.csv"
PREDICTION_SUMMARY = DATA / "prediction_summary.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a static website from the latest stock ranker report.")
    parser.add_argument("--site-dir", default=str(SITE), help="Static site output directory.")
    parser.add_argument("--limit", type=int, default=100, help="Rows to render on the static homepage.")
    return parser.parse_args()


def format_percent(value: str | None) -> str:
    number = parse_float(value)
    return "" if number is None else f"{number * 100:.2f}%"


def format_money(value: str | None) -> str:
    number = parse_float(value)
    if number is None:
        return ""
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:.2f} 億"
    if abs(number) >= 10_000:
        return f"{number / 10_000:.0f} 萬"
    return f"{number:,.0f}"


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def esc(value: object) -> str:
    import html

    return html.escape("" if value is None else str(value))


def next_business_day(value: str) -> str:
    current = datetime.strptime(value, "%Y-%m-%d").date()
    nxt = current
    while True:
        from datetime import timedelta

        nxt = nxt + timedelta(days=1)
        if nxt.weekday() < 5:
            return nxt.isoformat()


def load_performance_summary() -> dict[str, object] | None:
    if not PREDICTION_SUMMARY.exists():
        return None
    try:
        return json.loads(PREDICTION_SUMMARY.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def format_rate(value: object) -> str:
    number = parse_float(None if value is None else str(value))
    return "尚無" if number is None else f"{number * 100:.1f}%"


def format_signed_percent(value: object) -> str:
    number = parse_float(None if value is None else str(value))
    if number is None:
        return "尚無"
    return f"{number * 100:+.2f}%"


def render_performance_panel(summary: dict[str, object] | None) -> str:
    if not summary:
        return """
      <section class="performance-panel">
        <div class="performance-head">
          <div>
            <h2>預測勝負統計</h2>
            <p class="small">尚未累積可結算資料；每天盤後更新後會自動記錄並等待隔日收盤結算。</p>
          </div>
        </div>
      </section>
        """

    buckets = summary.get("buckets", {}) if isinstance(summary.get("buckets"), dict) else {}
    top1 = buckets.get("top_1", {}) if isinstance(buckets.get("top_1"), dict) else {}
    top3 = buckets.get("top_3", {}) if isinstance(buckets.get("top_3"), dict) else {}
    top10 = buckets.get("top_10", {}) if isinstance(buckets.get("top_10"), dict) else {}
    top30 = buckets.get("top_30", {}) if isinstance(buckets.get("top_30"), dict) else {}
    pending = summary.get("pending", 0)

    def stat(label: str, value: str, hint: str = "") -> str:
        return (
            "<div class='perf-stat'>"
            f"<span>{esc(label)}</span><strong>{esc(value)}</strong>"
            f"<small>{esc(hint)}</small>"
            "</div>"
        )

    stats = "".join(
        [
            stat("Top1 勝率", format_rate(top1.get("win_rate")), f"{top1.get('settled', 0)} 筆已結算"),
            stat("Top3 勝率", format_rate(top3.get("win_rate")), f"{top3.get('wins', 0)} 勝 / {top3.get('losses', 0)} 負"),
            stat("Top10 勝率", format_rate(top10.get("win_rate")), f"{top10.get('settled', 0)} 筆已結算"),
            stat("Top10 平均報酬", format_signed_percent(top10.get("avg_close_return")), "收盤到隔日收盤"),
            stat("Top30 勝率", format_rate(top30.get("win_rate")), f"{top30.get('settled', 0)} 筆已結算"),
            stat("待結算", str(pending), "等待下一個交易日收盤"),
        ]
    )

    daily_rows = []
    for item in (summary.get("recent_daily") or [])[:8]:
        if not isinstance(item, dict):
            continue
        result = item.get("top1_result", "")
        result_class = "win" if result == "win" else "loss" if result == "loss" else "tie"
        daily_rows.append(
            "<tr>"
            f"<td>{esc(item.get('prediction_date', ''))}</td>"
            f"<td>{esc(item.get('settled_date', ''))}</td>"
            f"<td>{esc(item.get('top1_stock', ''))}</td>"
            f"<td><mark class='result {result_class}'>{esc(result or '尚無')}</mark></td>"
            f"<td class='num'>{esc(format_signed_percent(item.get('top1_return')))}</td>"
            f"<td class='num'>{esc(format_rate(item.get('top3_win_rate')))}</td>"
            f"<td class='num'>{esc(format_rate(item.get('top10_win_rate')))}</td>"
            f"<td class='num'>{esc(format_signed_percent(item.get('top10_avg_return')))}</td>"
            "</tr>"
        )

    daily_table = ""
    if daily_rows:
        daily_table = f"""
        <div class="perf-table">
          <table>
            <thead>
              <tr>
                <th>預測日</th>
                <th>結算日</th>
                <th>Top1</th>
                <th>勝負</th>
                <th>Top1 報酬</th>
                <th>Top3 勝率</th>
                <th>Top10 勝率</th>
                <th>Top10 均報酬</th>
              </tr>
            </thead>
            <tbody>{''.join(daily_rows)}</tbody>
          </table>
        </div>
        """

    return f"""
      <section class="performance-panel">
        <div class="performance-head">
          <div>
            <h2>預測勝負統計</h2>
            <p class="small">勝負定義：預測日收盤到下一個交易日收盤，上漲為勝、下跌為負、平盤為平。</p>
          </div>
          <div class="performance-links">
            <a href="prediction-log.csv">完整紀錄 CSV</a>
            <a href="performance.json">統計 JSON</a>
          </div>
        </div>
        <div class="perf-stats">{stats}</div>
        {daily_table}
      </section>
    """


def render_static_index(csv_path: Path, limit: int) -> str:
    rows = load_rows(csv_path, limit)
    report_date = report_date_from_path(csv_path)
    target_date = next_business_day(report_date)
    updated = datetime.fromtimestamp(csv_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    performance_panel = render_performance_panel(load_performance_summary())
    signal_counts: dict[str, int] = {}
    for row in rows:
        signal = row.get("signal", "") or "未分類"
        signal_counts[signal] = signal_counts.get(signal, 0) + 1
    top_score = parse_float(rows[0].get("long_score")) if rows else None

    def stat(label: str, value: object, tone: str = "") -> str:
        return f"<div class='mini-stat {tone}'><span>{esc(label)}</span><strong>{esc(value)}</strong></div>"

    stats = "".join(
        [
            stat("最高分", "" if top_score is None else f"{top_score:.2f}", "good"),
            stat("偏多", signal_counts.get("偏多", 0), "good"),
            stat("觀察偏多", signal_counts.get("觀察偏多", 0), "watch"),
            stat("中性觀察", signal_counts.get("中性觀察", 0), ""),
        ]
    )

    table_rows: list[str] = []
    for row in rows:
        signal = row.get("signal", "")
        signal_class = "bull" if signal == "偏多" else "watch" if "觀察" in signal else "neutral"
        score = parse_float(row.get("long_score"))
        close = parse_float(row.get("close"))
        search_blob = " ".join(
            [
                row.get("stock_id", ""),
                row.get("name", ""),
                row.get("industry_category", "") or "",
                signal,
                row.get("reasons", "") or "",
            ]
        ).lower()
        table_rows.append(
            f"<tr data-signal='{esc(signal)}' data-search='{esc(search_blob)}'>"
            f"<td class='rank'>{esc(row.get('rank', ''))}</td>"
            f"<td><strong>{esc(row.get('stock_id', ''))}</strong><span>{esc(row.get('name', ''))}</span></td>"
            f"<td>{esc(row.get('industry_category', '') or '')}</td>"
            f"<td><mark class='{signal_class}'>{esc(signal)}</mark></td>"
            f"<td class='num score'>{'' if score is None else f'{score:,.2f}'}</td>"
            f"<td class='num'>{'' if close is None else f'{close:,.2f}'}</td>"
            f"<td class='num'>{format_percent(row.get('ret_1d'))}</td>"
            f"<td class='num'>{format_money(row.get('turnover'))}</td>"
            f"<td>{esc(row.get('reasons', '') or '')}</td>"
            "</tr>"
        )

    body = f"""
    <header class="topbar">
      <div>
        <p class="eyebrow">Taiwan Stock Next-Day Long Watchlist</p>
        <h1>台股隔日上漲候選排行</h1>
        <p class="subtitle">以 {esc(report_date)} 盤後資料，預測 {esc(target_date)} 的偏多觀察名單。</p>
      </div>
      <nav>
        <a href="download.csv">下載 CSV</a>
        <a href="report.md">Markdown 報告</a>
        <a href="prediction-log.csv">勝負紀錄</a>
        <a href="health.json">狀態</a>
      </nav>
    </header>

    <section class="summary">
      <div><span>資料日</span><strong>{esc(report_date)}</strong></div>
      <div><span>預測目標</span><strong>{esc(target_date)}</strong></div>
      <div><span>顯示檔數</span><strong>{len(rows)}</strong></div>
      <div><span>自動更新</span><strong>平日盤後</strong></div>
    </section>

    <main>
      <section class="insight-panel">
        <div>
          <h2>盤後摘要</h2>
          <p class="small">分數越高代表隔日偏多觀察優先度越高；仍需搭配隔日開盤、VWAP、流動性與風控確認。</p>
        </div>
        <div class="mini-stats">{stats}</div>
      </section>

      {performance_panel}

      <div class="table-head">
        <h2>候選清單</h2>
        <div class="tools" aria-label="排行篩選工具">
          <label class="search">
            <span>搜尋</span>
            <input id="searchBox" type="search" placeholder="代號、名稱、產業" autocomplete="off" />
          </label>
          <button id="clearSearch" class="clear-search" type="button">清除</button>
          <div class="chips" role="group" aria-label="訊號篩選">
            <button class="chip active" type="button" data-filter="all">全部</button>
            <button class="chip" type="button" data-filter="偏多">偏多</button>
            <button class="chip" type="button" data-filter="觀察偏多">觀察偏多</button>
            <button class="chip" type="button" data-filter="中性觀察">中性觀察</button>
          </div>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>股票</th>
              <th>產業</th>
              <th>訊號</th>
              <th>分數</th>
              <th>收盤</th>
              <th>日漲跌</th>
              <th>成交值</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>{''.join(table_rows)}</tbody>
        </table>
        <p id="emptyState" class="empty-state" hidden>沒有符合篩選條件的股票。</p>
      </div>
      <p class="note">這是量化候選排行，不是投資建議。實盤前請確認重大訊息、注意/處置、可當沖資格、券源、流動性與停損規則。</p>
    </main>
    """
    return html_page("台股隔日上漲候選排行", body)


def build_site(site_dir: Path, limit: int) -> tuple[Path, str]:
    csv_path = find_latest_csv()
    if csv_path is None:
        raise FileNotFoundError("找不到 outputs/tw_nextday_rank_*.csv，請先執行 python tw_stock_ranker.py")

    site_dir.mkdir(parents=True, exist_ok=True)
    report_date = report_date_from_path(csv_path)
    md_path = csv_path.with_suffix(".md")

    index_html = "\n".join(line.rstrip() for line in render_static_index(csv_path, limit).splitlines()) + "\n"
    (site_dir / "index.html").write_text(index_html, encoding="utf-8")
    shutil.copy2(csv_path, site_dir / "download.csv")
    if md_path.exists():
        shutil.copy2(md_path, site_dir / "report.md")
    else:
        (site_dir / "report.md").write_text(f"# 台股隔日上漲候選排行\n\n資料日：{report_date}\n", encoding="utf-8")

    performance = load_performance_summary()
    if PREDICTION_LOG.exists():
        shutil.copy2(PREDICTION_LOG, site_dir / "prediction-log.csv")
    if PREDICTION_SUMMARY.exists():
        shutil.copy2(PREDICTION_SUMMARY, site_dir / "performance.json")

    health = {
        "ok": True,
        "latest_csv": csv_path.name,
        "report_date": report_date,
        "target_date": next_business_day(report_date),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "auto_update": "GitHub Actions weekdays at 19:10, 20:10, 21:30, 22:30 Asia/Taipei",
    }
    if performance:
        buckets = performance.get("buckets", {}) if isinstance(performance.get("buckets"), dict) else {}
        top10 = buckets.get("top_10", {}) if isinstance(buckets.get("top_10"), dict) else {}
        health.update(
            {
                "prediction_rows": performance.get("rows", 0),
                "prediction_pending": performance.get("pending", 0),
                "prediction_top10_settled": top10.get("settled", 0),
                "prediction_top10_win_rate": top10.get("win_rate"),
            }
        )
    (site_dir / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")

    # Makes GitHub Pages serve files as-is even if Jekyll is enabled.
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")
    return site_dir, report_date


def main() -> int:
    args = parse_args()
    site_dir, report_date = build_site(Path(args.site_dir), args.limit)
    print(f"Built static site: {site_dir}")
    print(f"Report date: {report_date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
