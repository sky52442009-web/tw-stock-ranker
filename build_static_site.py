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


def render_static_index(csv_path: Path, limit: int) -> str:
    rows = load_rows(csv_path, limit)
    report_date = report_date_from_path(csv_path)
    target_date = next_business_day(report_date)
    updated = datetime.fromtimestamp(csv_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
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

      <div class="table-head">
        <h2>候選清單</h2>
        <div class="tools" aria-label="排行篩選工具">
          <label class="search">
            <span>搜尋</span>
            <input id="searchBox" type="search" placeholder="代號、名稱、產業" autocomplete="off" />
          </label>
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

    (site_dir / "index.html").write_text(render_static_index(csv_path, limit), encoding="utf-8")
    shutil.copy2(csv_path, site_dir / "download.csv")
    if md_path.exists():
        shutil.copy2(md_path, site_dir / "report.md")
    else:
        (site_dir / "report.md").write_text(f"# 台股隔日上漲候選排行\n\n資料日：{report_date}\n", encoding="utf-8")

    health = {
        "ok": True,
        "latest_csv": csv_path.name,
        "report_date": report_date,
        "target_date": next_business_day(report_date),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "auto_update": "GitHub Actions weekdays at 19:10, 20:10, 21:30, 22:30 Asia/Taipei",
    }
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
