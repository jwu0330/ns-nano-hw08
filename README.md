# HW08 Neurosynth 後端說明

## 繳交內容

1. **網址**：<https://ns-nano-hw08.onrender.com>
2. **程式**：`作業八_吳季穎_20261105.zip`（也公開在 <https://github.com/jwu0330/ns-nano-hw08>）

> 網站用 Render 免費方案，閒置一段時間會休眠，**第一次打開要等約 1 分鐘**，之後就正常。

## 操作方法

### 1. 直接看 API 結果

打開網址，首頁最上方的「API」區塊有範例連結，點了會在新分頁顯示 JSON：

| 作業要求的查詢 | 範例 |
|---|---|
| `/dissociate/terms/{詞A}/{詞B}`：提到 A、沒提到 B 的研究 | [PCC 但沒有 vmPFC](https://ns-nano-hw08.onrender.com/dissociate/terms/posterior_cingulate/ventromedial_prefrontal) |
| `/dissociate/locations/{x_y_z}/{x_y_z}`：在座標 A 有活化、在座標 B 沒有的研究 | [0_-52_26 但沒有 -2_50_-6](https://ns-nano-hw08.onrender.com/dissociate/locations/0_-52_26/-2_50_-6) |

JSON 裡的 `a_not_b.count` 是篇數，`a_not_b.studies` 是研究清單（PMID、標題、作者、期刊、年份）。

可加在網址後面的參數：

| 參數 | 作用 |
|---|---|
| `?both=true` | 同時回傳反方向（B 有、A 沒有） |
| `?analyze=true` | 加上兩邊各自的特徵詞與特徵腦區 |
| `?threshold=0.001` | 詞彙門檻（預設 0.001） |
| `?radius=6` | 座標半徑，單位 mm（預設 6） |
| `?limit=100&offset=0` | 分頁（預設每邊最多列 1,000 篇） |

### 2. 用網頁查詢

首頁下方是查詢介面：

1. 選「By terms」（用詞）或「By locations」（用座標）。
2. 填 A 和 B，按「Dissociate」；或直接按「Examples」裡的範例。
3. 結果會顯示兩邊篇數、各自的特徵詞，以及研究清單（點標題連到 PubMed）。

### 3. 範本的檢查項目

- [/img](https://ns-nano-hw08.onrender.com/img)：顯示杏仁核動圖
- [/test_db](https://ns-nano-hw08.onrender.com/test_db)：顯示資料庫連線狀態與各表筆數

### 4. API 文件

[/docs](https://ns-nano-hw08.onrender.com/docs) 列出所有端點。點開一個 → 按「Try it out」→ 按「Execute」，就能直接試打（範例值已填好）。

## 做了什麼

- **作業要求**：照老師 [ns-nano](https://github.com/spiketren/ns-nano) 的規格，做出兩個解離查詢，網址格式與範本相同；資料存在 Render 的 PostgreSQL + PostGIS，網站部署在 Render。範本的 `/img`、`/test_db` 保留。
- **「提到」的定義**：
  - 詞：該研究的 TF-IDF 權重 > 0.001（Neurosynth 慣例）
  - 座標：該研究有活化點落在 6 mm 內（neurosynth 套件預設）
- **與範本不同的地方**（範本檔案本身都沒改）：
  - 用 FastAPI 取代 Flask（題目允許），好處是自動檢查參數、自動產生 `/docs`。
  - 範本的 `create_db.py` 沒把詞彙前綴去掉，照用會查不到詞 → 另寫 `load_db.py`。
  - 範本沒鎖套件版本，照裝會連不上資料庫 → 鎖定版本。
  - 範本的座標索引類型不適合 3D 距離查詢 → 換成 3D 索引，查詢從 634 ms 變 7 ms。
- **額外做的**：網頁查詢介面、特徵分析（`analyze=true`）、打錯詞時建議相近的詞。

## 結果是否正確

- 另寫一份只用 pandas 直接讀原始資料的對照程式（`tests/reference.py`），12 組查詢與網站結果逐篇比對，完全一致。
- 共 75 個自動測試，線上網站全部通過。
- 另請 AI 審查程式碼，找到的 14 個問題都已修正。

老師的 DMN 範例結果：

| 查詢 | A 有 B 沒有 | B 有 A 沒有 |
|---|---|---|
| 詞：posterior cingulate vs ventromedial prefrontal | 897 篇 | 283 篇 |
| 座標：[0, −52, 26] vs [−2, 50, −6] | 406 篇 | 424 篇 |

特徵分析顯示：只提到 PCC 的研究多與 default mode network 相關；只提到 vmPFC 的研究多與 reward、value、decision making 相關，與文獻一致。

## 程式檔案

| 檔案 | 內容 |
|---|---|
| `app.py` | 網站本體：API、輸入檢查、資料庫查詢 |
| `analysis.py` | 特徵分析（額外功能） |
| `load_db.py` | 把老師的資料灌進資料庫 |
| `benchmark.py` | 效能量測 |
| `static/` | 首頁 `index.html`、範本的 `amygdala.gif` |
| `tests/` | 自動測試與對照程式 |
| `requirements.txt`、`render.yaml`、`LICENSE` | 套件清單、Render 設定、授權 |

## 在自己電腦執行

需要一個有 PostGIS 的 PostgreSQL，以及老師 repo 的三個 parquet 檔。在 `ns-nano-hw08/` 資料夾裡：

```bash
pip install -r requirements.txt
export DB_URL="postgresql://使用者:密碼@主機:5432/資料庫"
export NS_DATA_DIR=/path/to/ns-nano      # 老師 repo 的資料夾
python load_db.py                         # 灌資料，約 3 分鐘
uvicorn app:app                           # 開啟 http://127.0.0.1:8000
```

跑測試：

```bash
pip install pandas==3.1.1 pyarrow==26.0.1 pytest==9.2.0 httpx==0.28.1 playwright==1.64.1
pytest                                                   # 測本機（需要 DB_URL、NS_DATA_DIR）
API_BASE=https://ns-nano-hw08.onrender.com pytest       # 測線上網站
```

## 限制

- 免費方案：網站閒置會休眠；資料庫 2026-12-04 到期。
- 研究清單每邊預設最多列 1,000 篇，`count` 永遠是完整總數。
- 特徵分析只是描述性的比較，沒有做統計校正。

## AI 使用說明

本作業使用 AI 程式助理（Claude Code）協助撰寫程式、部署、測試與起草說明；作業方向、方法取捨與最終內容由本人決定並檢查。
