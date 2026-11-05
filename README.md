# Neurosynth 功能解離後端說明

NTU Psychoinformatics Week 8 作業（HW08: Neurosynth Backend）

## 繳交項目

1. **網址**：<https://ns-nano-hw08.onrender.com>
   首頁是查詢介面，`/docs` 是 API 文件。免費方案閒置時會休眠，第一次連線可能要等約一分鐘。
2. **後端程式**：`作業八_吳季穎_20261105.zip`，只有程式；說明就是本文件，另外繳交。程式也公開在 <https://github.com/jwu0330/ns-nano-hw08>，該 repo 的 README 與本文件相同。

## 實作範圍

老師的 [ns-nano](https://github.com/spiketren/ns-nano) 要求兩個查詢：

- `/dissociate/terms/<A>/<B>`：提到詞 A、沒提到詞 B 的研究
- `/dissociate/locations/<x_y_z>/<x_y_z>`：在座標 A 有活化、在座標 B 沒有活化的研究

兩者都可以反過來查（B\A）。資料庫用 PostgreSQL + PostGIS，部署在 Render。

我用 FastAPI 實作這兩個查詢，網址格式與範本相同，範本的冒煙測試 `/img`、`/test_db` 也保留。另外加了：一次回傳兩個方向、門檻與半徑參數、研究的標題與年份、分頁、找出「兩側各自的特徵詞與特徵腦區」的分析、統一的錯誤訊息、網頁查詢介面。

## 與範本的差異

範本檔案都沒有修改。下表列出各檔案的處理方式：

| 範本檔案 | 處理 | 說明 |
|---|---|---|
| `check_db.py` | 原樣使用 | 用它檢查 Render 資料庫，12 項全部通過 |
| `create_db.py` | 不使用，另寫 `load_db.py` | 範本有三個問題，見下方 |
| `app.py` | 改寫成 FastAPI（同樣叫 `app.py`） | 路徑相同；`/terms/<t>/studies`、`/locations/<xyz>/studies` 在範本只是空殼，這裡實作 |
| `requirements.txt` | 改寫並鎖定版本 | 見下方第 2 點 |
| `amygdala.gif` | 原樣使用 | `/img` |

實作時發現範本的三個問題：

1. **詞彙前綴沒有去掉**：`create_db.py` 用 `^terms_[^_]*__` 去掉欄名前綴，但這個正規表示式對 `terms_abstract_tfidf__amygdala` 不會匹配。照範本灌出來的資料庫，詞彙會存成 `terms_abstract_tfidf__amygdala`，查 `amygdala` 會查不到。
2. **照範本安裝會連不上資料庫**：範本沒有鎖版本，現在安裝會拿到 SQLAlchemy 2.1。它預設的 PostgreSQL 驅動改成 psycopg 3，範本卻裝 psycopg2，連線時出現 `No module named 'psycopg'`。
3. **座標索引用錯類型**：範本用 2D GiST 索引，以及代表經緯度的 SRID 4326。但 MNI 座標的單位是毫米，查詢用的 `ST_3DDWithin` 也需要 n 維索引。實測同一個查詢，範本的索引要 634 ms，n 維索引只要 7 ms（見「效能」）。

## 程式檔案

zip 解開後是 `ns-nano-hw08/` 資料夾：

| 檔案 | 內容 |
|---|---|
| `app.py` | 網站本體：兩個解離端點、輸入檢查、SQL 查詢、資料庫連線、範本的 `/img`、`/test_db` |
| `analysis.py` | 延伸功能：特徵分析（`analyze=true`） |
| `load_db.py` | 把老師的三個 parquet 檔灌進資料庫 |
| `benchmark.py` | 效能量測（資料庫查詢計畫、網站延遲） |
| `static/` | 首頁 `index.html`、範本的 `amygdala.gif` |
| `tests/` | 自動測試；`reference.py` 是獨立對照組 |
| `requirements.txt`、`render.yaml`、`LICENSE` | 套件清單、Render 部署設定、授權（沿用範本的 MIT） |

## 方法

### 資料庫

`load_db.py` 把老師附的三個 parquet 檔灌進 PostgreSQL，表名與範本相同：

- 研究資料 14,371 篇
- 座標 507,891 個（存成 SRID 0 的三維點，建 n 維 GiST 索引）
- 詞彙權重 1,049,299 筆（只存權重 > 0 的部分，共 3,228 個詞）

資料先灌進暫存 schema，筆數與 parquet 核對一致後，才用一個交易換成正式的 `ns`，網站不會讀到灌到一半的資料。整個資料庫 239 MB，在 Render 免費方案的 1 GB 上限內。

### 「提到」的定義

| 查詢 | 預設 | 依據 | 參數 |
|---|---|---|---|
| 研究提到詞 A | TF-IDF 權重 > 0.001 | Neurosynth／NiMARE 的慣例門檻 | `threshold` |
| 研究提到座標 A | 至少一個座標落在 A 的 6 mm 內（含 6 mm） | neurosynth 套件 `get_studies(r=6)` 的預設 | `radius` |

A\B 就是「提到 A 的研究」減去「提到 B 的研究」。單位是研究；這份資料每篇研究只有一個 contrast。

### API

| 端點 | 內容 |
|---|---|
| `/dissociate/terms/{A}/{B}`、`/dissociate/locations/{A}/{B}` | 作業要求的兩個查詢 |
| `/terms/{t}/studies`、`/locations/{xyz}/studies` | 單一詞或單一座標的研究 |
| `/terms?q=` | 詞彙搜尋（網頁的自動完成用） |
| `/`、`/img`、`/test_db`、`/stats`、`/health`、`/docs` | 網頁、範本冒煙測試、已載入資料的摘要、健康檢查、API 文件 |

解離查詢的參數：`both=true` 一次回傳兩個方向，`analyze=true` 加上特徵分析，`limit`、`offset` 分頁（預設每個方向最多 1,000 篇）。回傳內容包含兩側人數、交集人數，以及每篇研究的 PMID、標題、作者、期刊、年份（新到舊排序）。

詞彙可以用空格或底線（`posterior_cingulate`），大小寫不拘。詞彙表外的詞回 404，並附上相近的詞（例如 `amygdale` 會建議 `amygdala`）。座標格式錯誤或超出範圍回 400，參數超出範圍回 422，資料庫暫時連不上回 503。所有錯誤都是 `{"error": {"message": ...}}` 的格式。

### 特徵分析（`analyze=true`）

這部分是作業沒有要求的延伸：比較 A\B 與 B\A 兩群研究，找出各自比較常提到的詞，以及比較常報告的腦區（以 10 mm 為一格）。方法是計算每個詞（或每一格）的對數勝算比（加 0.5 校正），依它的 z 分數排序，各列前 10 名。為了避免結果被極少數研究主導，一個詞至少要出現在該側 3 篇研究，以及該側 2% 的研究中。與查詢詞共用任何一個字的詞會被排除：例如查 posterior cingulate 時，*cingulate cortex* 這類同義詞會排在最前面，但沒有提供新資訊。特徵腦區只在詞彙查詢時計算，因為座標查詢的兩側本來就是由座標決定的。

這種計算在免費方案的資料庫裡做，每次要約 3 秒。所以 `load_db.py` 灌資料時，順便建一份稀疏矩陣索引存在資料庫中，網站啟動時讀進記憶體，之後每次分析約 7 ms。

### 網頁介面

首頁 `/` 用第 8 週教的 HTML、CSS、JavaScript `fetch` 做成：分頁切換詞彙或座標、範例按鈕、詞彙自動完成、兩側人數的比例條、兩側研究清單（連到 PubMed，可載入更多）、特徵詞與特徵腦區，以及支援深色模式。查詢條件寫在網址裡，複製網址就能分享同一個查詢。API 回傳的文字一律用 `textContent` 放進頁面，不當成 HTML 解析，避免上課範例 `4_ajax.html` 那種直接塞 `innerHTML` 的注入風險。

### 驗證

- **獨立對照組**：`tests/reference.py` 只用 pandas 和 numpy，直接從 parquet 算答案，不經過資料庫或 API 程式碼。測試拿 12 組查詢（6 組詞彙、6 組座標，涵蓋不同門檻、半徑、小數座標，以及剛好落在半徑邊界上的點）的 API 結果，與對照組逐篇比對，完全一致。特徵分析的每個計數也一樣比對過。
- **自動測試**：共 75 個，包含解析、錯誤處理、分頁、跨來源（CORS）標頭、稀疏索引，以及用無頭 Chrome 在淺色與深色模式各跑一次的網頁操作測試。同一套測試可以直接打線上網址（設 `API_BASE`），最終版本部署後 75 個全部通過。
- **重現性**：從 GitHub 全新 clone、全新環境，只照 README 的步驟執行，測試全部通過。
- **獨立程式碼審查**：另請一個只讀程式碼、不知道我結論的 AI 審查者檢查，發現的問題見「審查與修正」。

## 結果

### 預設模式網路（DMN）範例

| 查詢 | 提到 A | 提到 B | 兩者都有 | A\B | B\A |
|---|---|---|---|---|---|
| 詞彙：posterior cingulate vs ventromedial prefrontal | 947 | 333 | 50 | **897** | **283** |
| 座標：[0, −52, 26] vs [−2, 50, −6]（6 mm） | 479 | 497 | 73 | **406** | **424** |

特徵分析的前幾名（z 分數）：

| 查詢 | A\B 的特徵詞 | B\A 的特徵詞 |
|---|---|---|
| 詞彙 | precuneus 7.7、network 7.4、default mode 6.1 | reward 8.1、value 7.6、decision making 6.6 |
| 座標 | mild cognitive 3.0、default mode 2.8、alzheimer 2.8 | ventromedial 4.4、orbitofrontal 3.7、anterior cingulate 3.2 |

- 用詞彙查詢時，PCC 這一側的特徵詞是 precuneus 和 default mode network，特徵腦區集中在 [0, −50, 30] 附近；vmPFC 這一側是 reward、value、decision making，特徵腦區在 [10, 30, −10]、[0, 40, −20] 附近。這與文獻中 PCC 參與自我參照與預設網路、vmPFC 參與價值判斷的分工一致。
- 用座標查詢時，PCC 座標這一側較常出現輕度認知障礙與阿茲海默症的研究，與 PCC 是阿茲海默症早期受影響區域的文獻一致。
- 座標查詢的 z 分數比詞彙查詢低，因為兩側各只有約 400 篇研究，而且同一個座標本來就會出現在各種不同主題的研究裡。

### 效能

資料庫端（`EXPLAIN ANALYZE`，7 次取中位數）：

| 查詢 | 時間 |
|---|---|
| 詞彙查詢（主鍵索引） | 2.0 ms |
| 座標查詢，n 維 GiST（本作業） | 7.0 ms |
| 座標查詢，2D GiST（範本） | 634 ms |
| 座標查詢，不用索引 | 6,097 ms |

從台灣連線上網址（各 20 次，p50／p95）：

| 請求 | 總時間 | 伺服器端 |
|---|---|---|
| 座標解離，每次不同查詢 | 217 / 648 ms | 117 / 555 ms |
| 詞彙解離，每次不同查詢 | 474 / 930 ms | 363 / 595 ms |
| DMN 座標解離，重複查詢 | 207 / 412 ms | 118 / 338 ms |
| DMN 詞彙解離 + 特徵分析 | 398 / 700 ms | 301 / 614 ms |

特徵分析改用記憶體索引之前，同一個請求的總時間 p50 是 2,430 ms。

## 審查與修正

功能完成後做了四種檢查：實際用瀏覽器看畫面、部署、用 22 種異常網址做黑箱測試，以及請一個只讀程式碼的 AI 審查者獨立檢查。審查者找到的 10 個問題修完後，再請它驗證一次，它又指出 4 個修正不完整的地方，也一併修正。以下是主要問題：

| 來源 | 問題 | 修正 |
|---|---|---|
| 看畫面 | 特徵腦區的 z 分數全是 NaN：網格座標的欄位名 `z` 與 z 分數同名，互相覆蓋 | 改欄位名；測試加上欄位檢查 |
| 部署 | `requirements.txt` 漏了 numpy，部署失敗（Render 保留舊版，網站沒有中斷） | 補上；部署前先在只裝 requirements 的乾淨環境試 import |
| 黑箱測試 | 搜尋 `%` 會被當成 SQL 萬用字元；5,000 字元的詞會被原樣回傳 | 只接受文字字元；限制長度 |
| 審查 | 若 PostGIS 裝在 `ns` schema，重灌時替換 schema 會把它一起刪掉 | 指定裝在 `public`；替換前檢查，灌完再實際查一次 |
| 審查 | 結果快取最多 1,024 筆、以浮點數為鍵，最壞約 1.8 GB，免費方案只有 512 MB | 上限改為 32 筆 |
| 審查 | 10 mm 方格用 `np.rint`（四捨六入五成雙），11% 剛好在格線上的座標都偏向 20 的倍數；對照組用同一個函式，所以測不出來 | 改成五一律進位；對照組改用「是否落在區間內」的不同寫法 |
| 審查 | 網頁的「Show more」讀的是目前表單的內容，改過表單再按，就會把別的查詢結果接在舊清單後面 | 改用畫面上那個查詢的網址 |
| 審查 | 每次開首頁都會跑 4 個 `count(*)`（其中一個 105 萬列） | 新增輕量的 `/stats` |
| 驗證 | 所有資料庫錯誤都回 503「請重試」，連程式本身的錯誤也被藏起來 | 只有連線類錯誤回 503；其他回 500 並記錄完整錯誤 |
| 最後看畫面 | 右側卡片的「這側／另一側」百分比顛倒 | 修正；網頁測試加上「這側 ≥ 另一側」的檢查 |

所有修正都重跑了全部測試（本機與線上）。

## 限制

- **免費方案的限制**：網站閒置 15 分鐘會休眠，喚醒約需一分鐘；資料庫 **2026-12-04 到期**（Render 免費資料庫的期限是 30 天），之後需要重建並重新灌資料。
- **「提到」用門檻判斷**：門檻預設 0.001，而資料中最小的正權重是 0.0114，所以預設門檻等於「摘要中出現這個詞」。座標半徑是固定的硬界線，邊界附近的研究會因為 1 mm 的差距被分到不同側。
- **特徵分析**：結果是描述性的，沒有做多重比較校正；縮寫（如 vmpfc）跟查詢詞沒有共用的字，所以不會被排除；腦區只用 10 mm 方格表示，沒有對應解剖名稱。
- **研究清單每個方向預設最多 1,000 篇**：`count` 永遠是完整總數，要更多可用 `limit`（上限 20,000）與 `offset`；作業的 DMN 例子都在 1,000 篇以內，會完整列出。
- **重灌資料後要重啟網站**：網站會快取查詢結果與分析索引。若沒有重啟，分析請求會偵測到資料不一致，回 503 並自動清掉快取，重試即可恢復。

## 執行方式

- **線上**：直接開網址。API 範例：
  - `https://ns-nano-hw08.onrender.com/dissociate/terms/posterior_cingulate/ventromedial_prefrontal?both=true`
  - `https://ns-nano-hw08.onrender.com/dissociate/locations/0_-52_26/-2_50_-6?both=true&analyze=true`
- **本機**：需要一個有 PostGIS 的 PostgreSQL，以及老師 repo 的三個 parquet 檔。在 `ns-nano-hw08/` 裡執行：

  ```bash
  pip install -r requirements.txt
  export DB_URL="postgresql://使用者:密碼@主機:5432/資料庫"
  export NS_DATA_DIR=/path/to/ns-nano            # 老師 repo 的資料夾
  python load_db.py                               # 灌資料，約 3 分鐘
  uvicorn app:app                                 # 開啟 http://127.0.0.1:8000
  ```

- **測試**：另外安裝 `pip install pandas==3.1.1 pyarrow==26.0.1 pytest==9.2.0 httpx==0.28.1 playwright==1.64.1`，執行 `pytest`。設 `API_BASE=https://ns-nano-hw08.onrender.com` 就改測線上網站。
- **效能量測**：`python benchmark.py db`（資料庫）、`python benchmark.py http --base https://ns-nano-hw08.onrender.com`（網站）。
- **部署**：Render 免費方案，新加坡機房。啟動指令 `gunicorn app:app -k uvicorn_worker.UvicornWorker --workers 2`；資料庫連線字串只放在 Render 的環境變數 `DB_URL`，不在程式碼裡。資料庫的 IP 白名單只開放灌資料用的那台主機。
- **執行環境**：開發機 NVIDIA GB10（ARM）；Python 3.13（本機 3.13.13，Render 3.13.18）、FastAPI 0.162、SQLAlchemy 2.1、psycopg 3.3、PostgreSQL 17 + PostGIS。

## AI 使用說明

本作業使用 AI 程式助理（Claude Code）協助撰寫程式、部署、測試與起草說明；作業方向、方法取捨與最終內容由本人決定並檢查。
