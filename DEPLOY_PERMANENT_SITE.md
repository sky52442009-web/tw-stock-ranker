# 永久網站部署方式

目前專案已可輸出靜態網站到 `site/`，適合部署到 GitHub Pages、Cloudflare Pages、Netlify 或任何靜態主機。

## 方案 A：GitHub Pages

這是建議方案。部署後電腦關機也能看，GitHub Actions 會在台灣時間平日 19:10 自動重建排行。

1. 在 GitHub 建立一個新的 public repository，例如 `tw-stock-ranker`。
2. 在本機專案資料夾執行：

```powershell
git init
git add .
git commit -m "Initial Taiwan stock ranker site"
git branch -M main
git remote add origin https://github.com/YOUR_NAME/tw-stock-ranker.git
git push -u origin main
```

3. 到 GitHub repository 的 `Settings -> Pages`，把 Source 設為 `GitHub Actions`。
4. 到 `Actions` 頁籤，手動執行 `Deploy Taiwan Stock Ranker`。
5. 完成後網址會像：

```text
https://YOUR_NAME.github.io/tw-stock-ranker/
```

## 方案 B：Cloudflare Pages

如果你有 Cloudflare 帳號：

1. 建立 Pages project，連到 GitHub repository。
2. Build command：

```bash
pip install -r requirements.txt && python tw_stock_ranker.py --top 100 && python build_static_site.py --limit 100
```

3. Output directory：

```text
site
```

Cloudflare Pages 的自動排程需要另外用 GitHub Actions 或 Cloudflare Workers Trigger；若已用本專案內的 GitHub Actions，就可直接部署到 GitHub Pages。

## 本機先預覽靜態網站

```powershell
python tw_stock_ranker.py
python build_static_site.py
python -m http.server 8080 -d site
```

打開：

```text
http://127.0.0.1:8080
```
