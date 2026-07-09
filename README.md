# 台股隔日上漲候選排行器

這個工具把附件研究報告中的盤前選股邏輯做成每日可執行的 CLI。主目標是排行「隔日較有機會上漲的台灣股票」，不是自動下單，也不是保證預測。

核心想法：

- 先排除低流動性、過熱、長上影、收低於 VWAP、注意/處置等風險。
- 再用爆量、相對強度、收盤位置、均線趨勢、MACD、RSI、盤整後突破與產業熱度評分。
- 研究報告中「漲停高當沖比」較偏隔日反向短沖，對做多排行會被視為過熱扣分。

## 快速使用

```bash
python tw_stock_ranker.py --end-date 2026-06-30 --top 30
```

未指定 `--end-date` 時，會以今天為截止日，並自動從往前的日曆天數抓取資料。如果今天還沒有盤後資料，工具會使用區間內最新有資料的交易日。

輸出位置：

- `outputs/tw_nextday_rank_YYYYMMDD.csv`
- `outputs/tw_nextday_rank_YYYYMMDD.md`

## 每日勝負統計

網站會用 `data/prediction_log.csv` 長期保存每日預測紀錄。勝負定義為「預測日收盤價」到「下一個交易日收盤價」：

- `win`：隔日收盤上漲
- `loss`：隔日收盤下跌
- `tie`：隔日收盤平盤
- `pending`：尚未等到下一個交易日收盤資料

手動更新統計：

```bash
python track_prediction_results.py --ranking-dir outputs --history-file data/prediction_log.csv --summary-file data/prediction_summary.json --cache-dir data/cache --end-date 2026-07-07
```

GitHub Actions 會在每天盤後產生新排行後自動執行這一步，並把 `data/prediction_log.csv` 與 `data/prediction_summary.json` commit 回 repo，讓公開網站持續累積 Top1、Top3、Top10、Top30 勝率與平均報酬。

## 每天盤後執行

```bash
python tw_stock_ranker.py
```

若要重新下載資料而不使用快取：

```bash
python tw_stock_ranker.py --refresh
```

若外網頁面日期不是最新交易日，先重新產生排行：

```bash
python tw_stock_ranker.py --end-date 2026-07-06
```

未指定 `--end-date` 時會以今天為截止日；若當天尚未有盤後資料，會使用區間內最新有資料的交易日。

## 資料來源

價格資料預設使用官方盤後資料：

- TWSE：`https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX`
- TPEx：`https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes`

產業分類 metadata 使用 FinMind 的 `TaiwanStockInfo`，因為它可一次取得上市櫃股票的代號、名稱、產業與市場別。若 metadata 抓取失敗，排行仍會用價格資料繼續跑，只是產業分數會降低。

## 可選補充資料

研究報告建議納入當沖比、融資與注意/處置狀態。這些欄位各家來源格式不固定，所以工具提供 CSV 疊加。

當沖比：

```csv
date,stock_id,daytrade_ratio
2026-06-30,2330,0.36
```

融資：

```csv
date,stock_id,margin_buy_ratio,margin_balance_change_ratio
2026-06-30,2330,0.08,0.02
```

注意/處置/警示：

```csv
date,stock_id,attention_flag
2026-06-30,9999,1
```

執行範例：

```bash
python tw_stock_ranker.py --daytrade-file data/daytrade.csv --margin-file data/margin.csv --attention-file data/attention.csv
```

## 分數解讀

主要欄位：

- `long_score`：扣除風險後的隔日做多排行分數，越高越優先觀察。
- `raw_long_score`：未扣風險前的多頭分數。
- `risk_penalty`：過熱、低流動性或警示等扣分。
- `signal`：`偏多`、`觀察偏多`、`中性觀察`、`低優先` 或 `排除`。
- `reasons`：本檔股票進榜或扣分的主要原因。

建議把 `long_score` 當成盤前觀察清單，不要直接當買進訊號。實盤前仍應檢查重大訊息、注意/處置、可當沖資格、券源、當天大盤環境與自己的停損規則。

## 除錯與資料品質

程式預設要求同一天同時有 TWSE 與 TPEx 資料。如果只抓到其中一個市場，該日會被略過，不會拿來產生全市場排行；若既有快取是不完整資料，程式會嘗試重抓，重抓後仍不完整就刪除該髒快取。

快速用快取驗證最新報表：

```bash
python tw_stock_ranker.py --end-date 2026-07-06 --no-network
```

外網頁面已設定 `Cache-Control: no-store`，更新排行後重新整理瀏覽器即可看到最新報表日期。
