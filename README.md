# 多功能線上壓縮工具 AI 助理指引

## 專案概述

這是一個基於 Flask 的線上檔案壓縮/解壓縮工具，提供多層加密壓縮功能。專案採用前後端分離架構：
- 前端：純靜態 HTML/JS，使用 Tailwind CSS 進行樣式設計
- 後端：Flask API + MongoDB 儲存任務狀態
- 部署平台：Zeabur

## 核心組件

### 1. 後端服務 (`app.py`)
- MongoDB 連接管理：使用環境變數 `MONGO_URI` 進行配置
- 檔案處理：使用 `uploads/` 和 `outputs/` 目錄
- 支援的壓縮格式：ZIP、7Z、TAR (GZ/BZ2/XZ)

### 2. 前端界面 (`templates/index.html`)
- 分為編碼器(壓縮)和解碼器(解壓縮)兩個主要功能頁籤
- 使用 WebSocket 實時更新任務進度

## 開發環境設置

1. 安裝依賴：
```bash
pip install -r requirements.txt
```

2. 環境變數設置：
- `MONGO_URI`：MongoDB 連接字串（必需）

## 關鍵開發模式

1. **任務狀態管理**
- 使用 MongoDB 的 TTL 索引自動清理過期任務（1小時後）
- 任務進度使用 `update_task_progress()` 更新
- 任務日誌使用 `update_task_log()` 記錄

2. **檔案處理流程**
- 上傳的檔案會先獲得一個 UUID
- 壓縮/解壓過程中的臨時檔案會被追蹤並在完成後清理
- 密碼存儲在單獨的文字檔案中

3. **安全性考慮**
- 支援多層加密壓縮
- 可以為奇數層或指定層次添加加密
- 密碼使用 `generate_password()` 隨機生成

## 整合要點

1. **MongoDB 整合**
- 檢查 `tasks_collection` 的連接狀態
- 使用 TTL 索引管理任務生命週期
- 任務狀態更新需要處理連接異常

2. **檔案系統整合**
- 確保 `uploads/` 和 `outputs/` 目錄存在
- 檔案命名使用 UUID 前綴防止衝突
- 支援多種壓縮格式的自動識別

## 常見開發工作流程

1. **添加新的壓縮格式支援**：
- 在 `get_file_type()` 添加新格式識別
- 在壓縮/解壓函數中添加對應的處理邏輯
- 更新前端介面支援新格式

2. **任務狀態追蹤**：
- 使用 `update_task_progress()` 更新進度
- 使用 `update_task_log()` 記錄關鍵操作
- 檢查 MongoDB TTL 索引確保過期清理
