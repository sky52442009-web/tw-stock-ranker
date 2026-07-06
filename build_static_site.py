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


def render_static_index(csv_path: Path, limit: int) -> str:
    rows = load_rows(csv_path, limit)
    report_date = report_date_from_path(csv_path)
    updated = datetime.fromtimestamp(csv_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    table_rows: list[str] = []
    for row in rows:
        signal = row.get("signal", "")
        signal_class = "bull" if signal == "偏多" else "watch" if "觀察" in signal else "neutral"
        score = parse_float(row.get("long_score"))
        close = parse_float(row.get("close"))
        table_rows.append(
            "<tr>"
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
      </div>
      <nav>
        <a href="download.csv">下載 CSV</a>
        <a href="report.md">Markdown 報告</a>
        <a href="health.json">狀態</a>
      </nav>
    </header>

    <section class="summary">
      <div><span>資料日</span><strong>{esc(report_date)}</strong></div>
      <div><span>顯示檔數</span><strong>{len(rows)}</strong></div>
      <div><span>最後建置</span><strong>{esc(updated)}</strong></div>
      <div><span>部署型態</span><strong>Static</strong></div>
    </section>

    <main>
      <div class="table-head">
        <h2>候選清單</h2>
        <p class="small">永久託管版。資料由排程或手動部署更新。</p>
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
      </div>
      <p class="note">這是量化候選排行，不是投資建議。實盤前請確認重大訊息、注意/處置、可當沖資格、券源、流動性與停損規則。</p>
    </main>
    """
    page = html_page("台股隔日上漲候選排行", body)
    return page.replace("form {", ".small { color: var(--muted); margin: 0; }\n    form {")


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
        "built_at": datetime.now().isoformat(timespec="seconds"),
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
