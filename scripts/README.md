# 數據種子腳本使用指南

## 概述

`seed_data.py` 是一個數據種子腳本，用於為管理後台儀表板生成演示數據。

## 功能

### 生成的數據

- **總任務數：** 50 個（可配置）
- **時間範圍：** 最近 7 天（隨機分佈）
- **任務狀態分佈：**
  - ✅ 完成：60%
  - 🔄 處理中：20%
  - ❌ 失敗：15%
  - ⏳ 等待中：5%

### 數據特點

1. **折線圖數據：** 任務分佈在最近 7 天，展示活動趨勢
2. **圓環圖數據：** 完成的任務會生成真實的 GridFS 文件（0.5-50 MB）
3. **真實日誌：** 每個任務包含壓縮過程的詳細日誌
4. **隨機參數：** 壓縮格式、加密模式、迭代次數等隨機生成

---

## 使用方法

### 前提條件

1. MongoDB 數據庫已啟動並可訪問
2. `MONGO_URI` 環境變數已設置

### 基本用法

```bash
# 在項目根目錄執行
python scripts/seed_data.py
```

### 設置環境變數

**在 Linux/macOS:**
```bash
export MONGO_URI="mongodb+srv://username:password@cluster.mongodb.net/"
python scripts/seed_data.py
```

**在 Windows (PowerShell):**
```powershell
$env:MONGO_URI="mongodb+srv://username:password@cluster.mongodb.net/"
python scripts/seed_data.py
```

**使用 .env 文件:**
```bash
# 確保 .env 文件包含 MONGO_URI
python -c "from dotenv import load_dotenv; load_dotenv()" && python scripts/seed_data.py
```

---

## 腳本行為

### 首次運行

腳本會檢查現有任務：
- 如果數據庫為空，直接生成 50 個任務
- 如果有現有數據，會詢問是否清除

```
⚠️  發現 X 個現有任務。是否清除？(y/N):
```

### 輸出示例

```
🌱 開始數據種子生成...
📊 目標：生成 50 個任務，分佈在最近 7 天

✅ MongoDB 連接成功

🧹 清理現有演示數據...
⚠️  發現 0 個現有任務。

📝 生成 50 個任務...
  進度：10/50 (20%)
  進度：20/50 (40%)
  進度：30/50 (60%)
  進度：40/50 (80%)
  進度：50/50 (100%)

💾 寫入數據庫...
✅ 成功插入 50 個任務

📊 數據統計：
--------------------------------------------------
  總任務數：50
  完成：30 (60.0%)
  處理中：10 (20.0%)
  失敗：8 (16.0%)
  等待中：2 (4.0%)
  總儲存空間：456.32 MB
--------------------------------------------------

📅 日期分佈（最近 7 天）:
--------------------------------------------------
  6天前 (2025-12-01): 8 個任務
  5天前 (2025-12-02): 6 個任務
  4天前 (2025-12-03): 7 個任務
  3天前 (2025-12-04): 9 個任務
  2天前 (2025-12-05): 5 個任務
  昨天 (2025-12-06): 7 個任務
  今天 (2025-12-07): 8 個任務
--------------------------------------------------

🎉 數據種子生成完成！

📌 下一步：
1. 訪問管理後台: http://localhost:5000/admin?secret=YOUR_ADMIN_SECRET
2. 查看折線圖展示最近 7 天的任務趨勢
3. 查看圓環圖展示儲存空間使用情況
4. 重新整理頁面以查看實時數據
```

---

## 配置選項

編輯 `scripts/seed_data.py` 中的配置參數：

```python
# 配置參數
TOTAL_TASKS = 50  # 總任務數（可修改為 10, 100, 500 等）
DAYS_TO_POPULATE = 7  # 填充最近 N 天的數據

# 任務狀態分佈（百分比）
STATUS_DISTRIBUTION = {
    '完成': 0.60,      # 60% 完成
    '處理中': 0.20,    # 20% 處理中
    '失敗': 0.15,      # 15% 失敗
    '等待中': 0.05     # 5% 等待中
}

# 文件大小範圍（MB）
FILE_SIZE_RANGE = (0.5, 50.0)  # 0.5 MB 到 50 MB
```

---

## 在生產環境運行

### Zeabur 部署環境

如果您的應用部署在 Zeabur：

```bash
# 1. SSH 進入容器
zeabur ssh <service-name>

# 2. 運行腳本
python scripts/seed_data.py
```

### Docker 環境

```bash
# 進入 Docker 容器
docker exec -it <container-name> bash

# 運行腳本
python scripts/seed_data.py
```

---

## 驗證結果

運行腳本後：

1. **訪問管理後台**
   ```
   http://localhost:5000/admin?secret=YOUR_ADMIN_SECRET
   ```

2. **檢查折線圖**
   - 應顯示最近 7 天的任務分佈
   - 數據點應該有變化（不是全部為 0）

3. **檢查圓環圖**
   - 顯示真實的儲存空間使用情況
   - 中央百分比應該 > 0%

4. **檢查統計卡片**
   - 總任務數應該是 50
   - 儲存空間應該顯示實際使用量（例如 456 MB）

---

## 清除測試數據

如果需要清除生成的測試數據：

```python
# 方法 1：重新運行腳本並選擇清除
python scripts/seed_data.py
# 當提示時輸入 'y'

# 方法 2：手動清除 MongoDB
mongo <your-connection-string>
use compressor_db
db.tasks.deleteMany({})
db.fs.files.deleteMany({})
db.fs.chunks.deleteMany({})
```

---

## 故障排除

### 問題：❌ 錯誤：無法連接到 MongoDB

**解決方法：**
1. 檢查 `MONGO_URI` 環境變數是否設置
2. 驗證 MongoDB 連接字符串格式正確
3. 確認網絡可以訪問 MongoDB 服務器

### 問題：文件導入錯誤

**解決方法：**
```bash
# 確保從項目根目錄運行
cd /home/user/zip
python scripts/seed_data.py
```

### 問題：權限錯誤

**解決方法：**
```bash
# 給予執行權限
chmod +x scripts/seed_data.py
```

---

## 注意事項

1. **生產環境使用：** 謹慎在生產環境使用，建議先在測試環境驗證
2. **儲存空間：** 生成 50 個任務可能會佔用 200-500 MB 的 GridFS 儲存空間
3. **MongoDB 免費層：** MongoDB Atlas 免費層限制 512 MB，注意不要超額
4. **數據清理：** 測試完成後記得清理數據以釋放空間

---

## 技術細節

### 使用的技術

- **Flask App Context：** 使用 `app.app_context()` 訪問應用配置
- **MongoDB 操作：** 使用 `tasks_collection` 和 GridFS
- **隨機數據生成：** 使用 Python `random` 模組
- **日期處理：** 使用 `datetime` 和 `timedelta`

### 數據結構

生成的任務文檔包含：
- `_id`: MongoDB ObjectId
- `status`: 任務狀態
- `progress`: 進度百分比（0-100）
- `logs`: 處理日誌數組
- `params`: 壓縮參數
- `created_at`: 創建時間（分佈在最近 7 天）
- `result_file_id`: GridFS 文件 ID（僅完成狀態）
- `password_file_content`: 密碼列表（僅完成狀態）

---

**版本：** 1.0
**最後更新：** 2025-12-07
**兼容版本：** v2.1.0+
