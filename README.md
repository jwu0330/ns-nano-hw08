# Neurosynth 功能解離後端

NTU Psychoinformatics HW08

網址：<https://ns-nano-hw08.onrender.com>
程式：`作業八_吳季穎_20261105.zip`，同時放在 GitHub：<https://github.com/jwu0330/ns-nano-hw08>

網站使用 Render 免費方案，閒置後會進入休眠，第一次開啟約需等待一分鐘。

## 使用方式

首頁上方列出兩個查詢的網址格式與範例連結，點選後會顯示 JSON 結果：

- 詞彙查詢 `/dissociate/terms/{詞A}/{詞B}`：回傳提到詞 A、但沒有提到詞 B 的研究。
  範例：<https://ns-nano-hw08.onrender.com/dissociate/terms/posterior_cingulate/ventromedial_prefrontal>
- 座標查詢 `/dissociate/locations/{x_y_z}/{x_y_z}`：回傳在座標 A 附近有活化、但在座標 B 附近沒有活化的研究。
  範例：<https://ns-nano-hw08.onrender.com/dissociate/locations/0_-52_26/-2_50_-6>

回傳結果中，`a_not_b.count` 是研究篇數，`a_not_b.studies` 是研究清單，包含 PMID、標題、作者、期刊與年份。網址後面可以加上參數：`both=true` 同時查詢反方向，`threshold` 與 `radius` 調整判定標準，`limit` 與 `offset` 用於分頁（預設每個方向最多列出 1,000 篇）。

首頁下方是查詢介面。選擇用詞彙或座標查詢，輸入 A 與 B 後按 Dissociate，或直接按範例按鈕，就會顯示兩邊的篇數與研究清單。範本要求的 `/img` 與 `/test_db` 都可以正常開啟，所有端點的說明與試用介面在 `/docs`。

## 實作內容

依照老師的 ns-nano 範本，把 Neurosynth 資料匯入 Render 上的 PostgreSQL（PostGIS），並用 FastAPI 實作兩個查詢，網址格式與範本相同。判定標準採用 Neurosynth 的慣例：研究的詞彙 TF-IDF 權重大於 0.001，視為提到該詞；研究有活化點落在座標 6 mm 以內，視為提到該座標。

範本的檔案都沒有修改，但實作時發現三個地方需要另外處理：

1. `create_db.py` 沒有去掉詞彙欄名的前綴，照原樣匯入後查不到任何詞彙，所以另寫了 `load_db.py`。
2. `requirements.txt` 沒有固定版本，現在安裝到的 SQLAlchemy 預設驅動與範本不同，會無法連線，所以固定了版本。
3. 座標用的 2D 索引不適合三維距離查詢，改用 3D 索引後，同一個查詢從 634 ms 縮短為 7 ms。

除了作業要求的部分，另外加上網頁查詢介面，以及比較兩組研究各自常見詞彙與腦區的分析（網址加上 `analyze=true`）。

## 驗證

另外寫了一個只用 pandas 直接讀取原始資料的對照程式（`tests/reference.py`），用 12 組查詢與網站結果逐篇比對，結果完全一致。全部 75 個自動測試在線上網站都通過。程式碼也另外請 AI 審查，發現的問題都已修正。

老師的預設模式網路範例結果如下：

| 查詢 | A 有、B 沒有 | B 有、A 沒有 |
|---|---|---|
| 詞彙：posterior cingulate／ventromedial prefrontal | 897 篇 | 283 篇 |
| 座標：[0, −52, 26]／[−2, 50, −6] | 406 篇 | 424 篇 |

只提到 PCC 的研究多與 default mode network 有關，只提到 vmPFC 的研究多與 reward、value、decision making 有關，與文獻相符。

## 程式檔案

`app.py` 是網站本體，`analysis.py` 是額外的分析功能，`load_db.py` 負責把資料匯入資料庫，`benchmark.py` 用來量測效能，`static/` 放首頁與範本圖片，`tests/` 是測試程式。

## 本機執行

需要一個有 PostGIS 的 PostgreSQL，以及老師 repo 中的三個 parquet 檔。在程式資料夾中執行：

```bash
pip install -r requirements.txt
export DB_URL="postgresql://使用者:密碼@主機:5432/資料庫"
export NS_DATA_DIR=/path/to/ns-nano      # 老師 repo 的資料夾
python load_db.py                         # 匯入資料，約 3 分鐘
uvicorn app:app                           # 開啟 http://127.0.0.1:8000
```

執行測試：

```bash
pip install pandas==3.1.1 pyarrow==26.0.1 pytest==9.2.0 httpx==0.28.1 playwright==1.64.1
pytest                                                # 測試本機
API_BASE=https://ns-nano-hw08.onrender.com pytest    # 測試線上網站
```

## 限制

Render 免費方案的網站閒置後會休眠，資料庫將於 2026-12-04 到期。分析功能只是描述性的比較，沒有做統計校正。

## AI 使用說明

本作業使用 AI 程式助理（Claude Code）協助撰寫程式、部署、測試與起草說明；作業方向、方法取捨與最終內容由本人決定並檢查。
