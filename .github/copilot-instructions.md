# 多功能線上壓縮工具 AI 助理指引

## 專案概述

這是一個基於 Flask 的線上檔案壓縮/解壓縮工具，提供多層加密壓縮功能。專案採用前後端分離架構：
- 前端：純靜態 HTML/JS，使用 Tailwind CSS 進行樣式設計
- 後端：Flask API + MongoDB 儲存任務狀態
- 部署平台：Zeabur

## 核心組件

### 1. 後端服務 (`app.py`)
- MongoDB 連接管理：
  - 使用環境變數 `MONGO_URI` 進行配置
  - 資料庫名稱強制設定為 'compressor_db'
  - 使用 `tasks` 集合儲存任務狀態
- 檔案處理：
  - 上傳目錄：`uploads/`（臨時檔案）
  - 輸出目錄：`outputs/`（結果檔案）
- 支援的壓縮格式：
  ```python
  formats = {
    'zip': '.zip',    # 使用 py7zr
    '7z': '.7z',      # 使用 py7zr
    'targz': '.tar.gz',   # 使用 tarfile
    'tarbz2': '.tar.bz2', # 使用 tarfile
    'tarxz': '.tar.xz'    # 使用 tarfile
  }
  ```

### 2. 前端界面 (`templates/index.html`)
- 分為編碼器(壓縮)和解碼器(解壓縮)兩個主要功能頁籤
- 使用輪詢機制（每2秒）更新任務進度：`/status/{task_id}`
- 提供進度條和日誌面板實時顯示處理狀態

## 開發環境設置

1. 安裝依賴：
```bash
pip install -r requirements.txt
```

2. 環境變數設置：
```bash
# 本地開發時的設定
MONGO_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/compressor_db
```

## 關鍵開發模式

1. **任務狀態管理**
- 資料模型：
  ```python
  task = {
      'status': '處理中',      # 狀態：處理中、完成、失敗
      'progress': 0,          # 進度：0-100
      'logs': [],            # 日誌記錄列表
      'params': {},          # 任務參數
      'created_at': datetime # 建立時間
  }
  ```
- 使用 `update_task_progress(task_id, progress)` 更新進度
- 使用 `update_task_log(task_id, message)` 記錄日誌

2. **檔案處理流程**
- 檔案命名慣例：
  - 上傳檔案：保持原檔名
  - 處理中檔案：`{task_id}_secure_layer_{i}.{ext}`
  - 密碼文件：文本格式，包含每層壓縮的密碼
- 檔案清理：
  - 上傳檔案：處理完成後刪除
  - 中間檔案：下一層壓縮完成後刪除
  - 輸出檔案：保留供下載

3. **加密處理邏輯**
```python
# 支援兩種加密模式：
odd_layers = params['encrypt_odd']        # 加密所有奇數層
manual_layers = params['manual_layers']   # 手動指定加密層

# 加密判斷邏輯
should_encrypt = (odd_layers and i % 2 != 0) or (not odd_layers and i in manual_layers)
if should_encrypt:
    password = generate_password()  # 12位隨機密碼
```

## 整合要點

1. **MongoDB 整合**
- 連接檢查：
  ```python
  client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
  client.admin.command('ping')  # 驗證連接
  ```
- 錯誤處理：
  - 確保每個資料庫操作都有異常捕捉
  - 在 API 響應中反映資料庫錯誤

2. **檔案系統整合**
- 檔案格式辨識：
  ```python
  def get_file_type(filename):
      if filename.endswith('.zip'): return 'zip'
      if filename.endswith('.7z'): return '7z'
      if filename.endswith(('.tar.gz', '.tgz')): return 'targz'
      # ... 更多格式
  ```
- 檔案操作時的關鍵點：
  - 使用 `os.makedirs(..., exist_ok=True)` 確保目錄存在
  - 在檔案操作完成後立即清理臨時檔案

## 常見開發工作流程

1. **新增壓縮格式支援**：
```python
# 1. 在 formats 字典中添加新格式
formats['新格式'] = '.副檔名'

# 2. 在 get_file_type() 中添加識別
if filename.endswith('.新副檔名'): return '新格式'

# 3. 在壓縮工作器中添加處理邏輯
if format == '新格式':
    # 實現壓縮/解壓邏輯
```

2. **調試和監控**：
- 使用 `logging` 模組記錄關鍵操作：
  ```python
  logging.info("✅ 成功連線至 MongoDB！")
  logging.error(f"❌ 壓縮任務失敗: {e}")
  ```
- 使用任務日誌追蹤進度：
  ```python
  update_task_log(task_id, f"第 {i}/{iterations} 次壓縮")
  ```