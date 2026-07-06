from __future__ import annotations

import argparse
import csv
import html
import json
import mimetypes
import os
import re
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"


def find_latest_csv() -> Path | None:
    files = list(OUTPUTS.glob("tw_nextday_rank_*.csv"))
    if not files:
        return None

    def sort_key(path: Path) -> tuple[str, float]:
        match = re.search(r"(\d{8})", path.stem)
        return (match.group(1) if match else "00000000", path.stat().st_mtime)

    return sorted(files, key=sort_key, reverse=True)[0]


def report_date_from_path(path: Path) -> str:
    match = re.search(r"(\d{8})", path.stem)
    if not match:
        return "unknown"
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return match.group(1)


def load_rows(path: Path, limit: int) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for row in reader:
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


def safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def fmt_pct(value: str | None) -> str:
    number = safe_float(value)
    if number is None:
        return ""
    return f"{number * 100:.2f}%"


def fmt_money(value: str | None) -> str:
    number = safe_float(value)
    if number is None:
        return ""
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:.2f} 億"
    if abs(number) >= 10_000:
        return f"{number / 10_000:.0f} 萬"
    return f"{number:,.0f}"


def fmt_num(value: str | None, digits: int = 2) -> str:
    number = safe_float(value)
    if number is None:
        return html.escape(value or "")
    return f"{number:,.{digits}f}"


def render_home(limit: int) -> bytes:
    csv_path = find_latest_csv()
    if csv_path is None:
        body = """
        <main class="empty">
          <h1>台股隔日上漲候選排行</h1>
          <p>目前找不到輸出檔。請先在本機執行 <code>python tw_stock_ranker.py</code> 產生排行。</p>
        </main>
        """
        return html_page("台股隔日上漲候選排行", body).encode("utf-8")

    rows = load_rows(csv_path, limit)
    report_date = report_date_from_path(csv_path)
    updated = datetime.fromtimestamp(csv_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    table_rows = []
    for row in rows:
        signal = row.get("signal", "")
        signal_class = "bull" if signal == "偏多" else "watch" if "觀察" in signal else "neutral"
        table_rows.append(
            "<tr>"
            f"<td class='rank'>{html.escape(row.get('rank', ''))}</td>"
            f"<td><strong>{html.escape(row.get('stock_id', ''))}</strong><span>{html.escape(row.get('name', ''))}</span></td>"
            f"<td>{html.escape(row.get('industry_category', '') or '')}</td>"
            f"<td><mark class='{signal_class}'>{html.escape(signal)}</mark></td>"
            f"<td class='num score'>{fmt_num(row.get('long_score'))}</td>"
            f"<td class='num'>{fmt_num(row.get('close'))}</td>"
            f"<td class='num'>{fmt_pct(row.get('ret_1d'))}</td>"
            f"<td class='num'>{fmt_money(row.get('turnover'))}</td>"
            f"<td>{html.escape(row.get('reasons', '') or '')}</td>"
            "</tr>"
        )

    body = f"""
    <header class="topbar">
      <div>
        <p class="eyebrow">Taiwan Stock Next-Day Long Watchlist</p>
        <h1>台股隔日上漲候選排行</h1>
      </div>
      <nav>
        <a href="/download.csv">下載 CSV</a>
        <a href="/report.md">Markdown 報告</a>
        <a href="/health">狀態</a>
      </nav>
    </header>

    <section class="summary">
      <div><span>資料日</span><strong>{html.escape(report_date)}</strong></div>
      <div><span>顯示檔數</span><strong>{len(rows)}</strong></div>
      <div><span>最後更新</span><strong>{html.escape(updated)}</strong></div>
      <div><span>資料夾</span><strong>outputs</strong></div>
    </section>

    <main>
      <div class="table-head">
        <h2>候選清單</h2>
        <form method="get">
          <label>顯示前
            <input name="n" value="{limit}" inputmode="numeric" />
          </label>
          <button type="submit">套用</button>
        </form>
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
          <tbody>
            {''.join(table_rows)}
          </tbody>
        </table>
      </div>
      <p class="note">這是量化候選排行，不是投資建議。實盤前請確認重大訊息、注意/處置、可當沖資格、券源、流動性與停損規則。</p>
    </main>
    """
    return html_page("台股隔日上漲候選排行", body).encode("utf-8")


def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --text: #15171a;
      --muted: #65717f;
      --line: #dce2e8;
      --panel: #ffffff;
      --accent: #0f766e;
      --accent-2: #14532d;
      --warn: #92400e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Microsoft JhengHei", "Noto Sans TC", system-ui, sans-serif;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: end;
      padding: 28px clamp(16px, 4vw, 48px);
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }}
    .eyebrow {{
      margin: 0 0 6px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: .04em;
      text-transform: uppercase;
    }}
    h1, h2 {{ margin: 0; }}
    h1 {{ font-size: 30px; line-height: 1.2; }}
    h2 {{ font-size: 20px; }}
    nav {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    a, button {{
      color: var(--accent);
      background: #ecfdf5;
      border: 1px solid #99f6e4;
      border-radius: 8px;
      padding: 9px 12px;
      text-decoration: none;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 1px;
      margin: 0;
      background: var(--line);
      border-bottom: 1px solid var(--line);
    }}
    .summary div {{
      background: var(--panel);
      padding: 16px clamp(16px, 4vw, 48px);
    }}
    .summary span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 4px;
    }}
    .summary strong {{ font-size: 18px; }}
    main {{
      padding: 24px clamp(16px, 4vw, 48px) 44px;
    }}
    .table-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 12px;
    }}
    form {{ display: flex; gap: 8px; align-items: center; color: var(--muted); }}
    input {{
      width: 72px;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      font: inherit;
      margin-left: 6px;
    }}
    .table-wrap {{
      overflow-x: auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{
      width: 100%;
      min-width: 1040px;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #f9fafb;
      color: #374151;
      font-size: 12px;
      text-transform: uppercase;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    td span {{ display: block; color: var(--muted); margin-top: 2px; }}
    .rank, .num {{ text-align: right; white-space: nowrap; }}
    .score {{ color: var(--accent-2); font-weight: 800; }}
    mark {{
      display: inline-block;
      padding: 4px 8px;
      border-radius: 999px;
      font-weight: 700;
      background: #eef2ff;
      color: #3730a3;
    }}
    mark.bull {{ background: #dcfce7; color: #166534; }}
    mark.watch {{ background: #fef3c7; color: var(--warn); }}
    .note {{
      color: var(--muted);
      margin: 16px 0 0;
      line-height: 1.7;
    }}
    .empty {{ max-width: 760px; margin: 80px auto; line-height: 1.8; }}
    code {{ background: #e5e7eb; padding: 2px 6px; border-radius: 6px; }}
    @media (max-width: 760px) {{
      .topbar {{ display: block; }}
      nav {{ margin-top: 18px; }}
      .summary {{ grid-template-columns: 1fr 1fr; }}
      .table-head {{ align-items: stretch; flex-direction: column; }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


class RankerHandler(BaseHTTPRequestHandler):
    server_version = "TwRankerHTTP/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            query = parse_qs(parsed.query)
            try:
                limit = max(1, min(200, int(query.get("n", ["50"])[0])))
            except ValueError:
                limit = 50
            self.send_bytes(render_home(limit), "text/html; charset=utf-8")
            return
        if parsed.path == "/download.csv":
            self.serve_latest("csv")
            return
        if parsed.path == "/report.md":
            self.serve_latest("md")
            return
        if parsed.path == "/health":
            latest = find_latest_csv()
            payload = {
                "ok": latest is not None,
                "latest_csv": latest.name if latest else None,
                "report_date": report_date_from_path(latest) if latest else None,
            }
            self.send_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def serve_latest(self, suffix: str) -> None:
        csv_path = find_latest_csv()
        if csv_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No report has been generated yet")
            return
        path = csv_path if suffix == "csv" else csv_path.with_suffix(".md")
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, f"No {suffix} report found")
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if suffix == "md" else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        if os.environ.get("TW_RANKER_HTTP_LOG", "0") == "1":
            super().log_message(fmt, *args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the latest Taiwan stock ranker report as a small web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), RankerHandler)
    print(f"Serving on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
