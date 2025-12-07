#!/usr/bin/env python3
"""
數據種子腳本 - 為管理後台儀表板生成演示數據

此腳本生成合成任務數據以展示：
- 折線圖（最近 7 天的任務趨勢）
- 圓環圖（儲存空間使用情況）
- 統計卡片（總任務數、活躍任務等）

使用方法:
    python scripts/seed_data.py
"""

import sys
import os
from datetime import datetime, timedelta
import random

# 添加父目錄到 Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, tasks_collection, fs
from bson import ObjectId


# 配置參數
TOTAL_TASKS = 50  # 總任務數
DAYS_TO_POPULATE = 7  # 填充最近 7 天的數據

# 任務狀態分佈（百分比）
STATUS_DISTRIBUTION = {
    '完成': 0.60,      # 60% 完成
    '處理中': 0.20,    # 20% 處理中
    '失敗': 0.15,      # 15% 失敗
    '等待中': 0.05     # 5% 等待中
}

# 文件大小範圍（MB）
FILE_SIZE_RANGE = (0.5, 50.0)

# 壓縮格式選項
COMPRESSION_FORMATS = ['zip', '7z', 'targz', 'tarbz2', 'tarxz']

# 加密模式選項
ENCRYPT_MODES = ['odd', 'even', 'all', None]


def generate_random_date(days_ago_max=7):
    """生成最近 N 天內的隨機日期時間"""
    now = datetime.now()
    days_ago = random.uniform(0, days_ago_max)
    return now - timedelta(days=days_ago)


def generate_task_status():
    """根據分佈隨機選擇任務狀態"""
    rand = random.random()
    cumulative = 0
    for status, prob in STATUS_DISTRIBUTION.items():
        cumulative += prob
        if rand <= cumulative:
            return status
    return '完成'  # 預設返回完成


def generate_file_size_mb():
    """生成隨機文件大小（MB）"""
    return round(random.uniform(*FILE_SIZE_RANGE), 2)


def create_mock_task(task_number):
    """創建一個模擬任務文檔"""
    status = generate_task_status()
    created_at = generate_random_date(DAYS_TO_POPULATE)
    iterations = random.randint(1, 10)
    formats = random.sample(COMPRESSION_FORMATS, k=random.randint(1, 3))
    encrypt_mode = random.choice(ENCRYPT_MODES)
    file_size_mb = generate_file_size_mb()

    # 計算進度
    if status == '完成':
        progress = 100
        progress_text = '已完成'
    elif status == '處理中':
        progress = random.randint(10, 90)
        current_layer = int(iterations * progress / 100)
        progress_text = f'正在壓縮第 {current_layer}/{iterations} 層'
    elif status == '失敗':
        progress = random.randint(20, 80)
        progress_text = '處理失敗：壓縮錯誤'
    else:  # 等待中
        progress = 0
        progress_text = '等待處理'

    # 生成日誌
    logs = []
    if status in ['完成', '處理中', '失敗']:
        logs.append(f"--- 開始處理：{iterations} 層壓縮 ---")
        processed_layers = int(iterations * progress / 100) if progress < 100 else iterations
        for i in range(1, processed_layers + 1):
            format_name = formats[(i - 1) % len(formats)]
            logs.append(f"--- 正在壓縮第 {i}/{iterations} 層 (格式: {format_name}) ---")

        if status == '完成':
            logs.append(f"✅ 壓縮完成！最終文件大小: {file_size_mb} MB")
        elif status == '失敗':
            logs.append("❌ 壓縮失敗：檔案格式錯誤")

    # 創建任務文檔
    task = {
        "_id": ObjectId(),
        "status": status,
        "progress": progress,
        "progress_text": progress_text,
        "logs": logs,
        "params": {
            "original_file": f"/tmp/compressor_uploads/demo_file_{task_number}.bin",
            "iterations": iterations,
            "formats": formats,
            "encrypt_mode": encrypt_mode,
            "raw_filename": f"demo_file_{task_number}.dat"
        },
        "created_at": created_at,
        "result_filename": f"compressed_{task_number}.{'zip' if status == '完成' else 'tmp'}",
        "delete_token": ObjectId().hex,
        "cancel_requested": False
    }

    # 如果是完成狀態，模擬上傳 GridFS 文件
    if status == '完成':
        # 創建模擬文件內容
        mock_content = b'X' * int(file_size_mb * 1024 * 1024)  # 生成指定大小的內容
        file_id = fs.put(
            mock_content,
            filename=task['result_filename'],
            content_type='application/octet-stream',
            metadata={
                'task_id': str(task['_id']),
                'original_filename': task['params']['raw_filename'],
                'created_at': created_at
            }
        )
        task['result_file_id'] = str(file_id)

        # 添加密碼文件內容
        password_lines = []
        for i in range(1, iterations + 1):
            password = f"pwd{random.randint(1000, 9999)}" if encrypt_mode else "(無密碼)"
            format_name = formats[(i - 1) % len(formats)]
            password_lines.append(f"第 {i} 層 (layer_{i}.{format_name}): {password}")
        task['password_file_content'] = '\n'.join(password_lines)

    return task


def seed_database():
    """填充數據庫"""
    with app.app_context():
        print(f"🌱 開始數據種子生成...")
        print(f"📊 目標：生成 {TOTAL_TASKS} 個任務，分佈在最近 {DAYS_TO_POPULATE} 天")
        print()

        # 檢查數據庫連接
        try:
            from pymongo import MongoClient
            # 使用 app.config 中的 MongoDB URI
            if tasks_collection is None:
                print("❌ 錯誤：無法連接到 MongoDB")
                print("請確保 MONGO_URI 環境變數已設置")
                return

            print("✅ MongoDB 連接成功")
        except Exception as e:
            print(f"❌ 數據庫連接錯誤：{e}")
            return

        # 清除現有的演示數據（可選）
        print("\n🧹 清理現有演示數據...")
        existing_count = tasks_collection.count_documents({})
        if existing_count > 0:
            response = input(f"⚠️  發現 {existing_count} 個現有任務。是否清除？(y/N): ")
            if response.lower() == 'y':
                tasks_collection.delete_many({})
                # 清除 GridFS 文件
                for grid_out in fs.find():
                    fs.delete(grid_out._id)
                print("✅ 已清除現有數據")
            else:
                print("ℹ️  保留現有數據，將追加新任務")

        # 生成任務
        print(f"\n📝 生成 {TOTAL_TASKS} 個任務...")
        tasks = []
        status_counts = {status: 0 for status in STATUS_DISTRIBUTION.keys()}
        total_size_mb = 0

        for i in range(1, TOTAL_TASKS + 1):
            task = create_mock_task(i)
            tasks.append(task)
            status_counts[task['status']] += 1

            # 計算總文件大小
            if task['status'] == '完成' and 'result_file_id' in task:
                # 從 GridFS 獲取文件大小
                file_obj = fs.get(ObjectId(task['result_file_id']))
                total_size_mb += file_obj.length / (1024 * 1024)

            # 顯示進度
            if i % 10 == 0:
                print(f"  進度：{i}/{TOTAL_TASKS} ({i/TOTAL_TASKS*100:.0f}%)")

        # 批量插入任務
        print("\n💾 寫入數據庫...")
        result = tasks_collection.insert_many(tasks)
        print(f"✅ 成功插入 {len(result.inserted_ids)} 個任務")

        # 顯示統計
        print("\n📊 數據統計：")
        print("-" * 50)
        print(f"  總任務數：{TOTAL_TASKS}")
        for status, count in status_counts.items():
            percentage = count / TOTAL_TASKS * 100
            print(f"  {status}：{count} ({percentage:.1f}%)")
        print(f"  總儲存空間：{total_size_mb:.2f} MB")
        print("-" * 50)

        # 顯示日期分佈
        print("\n📅 日期分佈（最近 7 天）:")
        print("-" * 50)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(6, -1, -1):
            date = today - timedelta(days=i)
            date_str = date.strftime('%Y-%m-%d')
            count = sum(1 for task in tasks if task['created_at'].date() == date.date())
            if i == 0:
                label = '今天'
            elif i == 1:
                label = '昨天'
            else:
                label = f'{i}天前'
            print(f"  {label} ({date_str}): {count} 個任務")
        print("-" * 50)

        print("\n🎉 數據種子生成完成！")
        print("\n📌 下一步：")
        print("1. 訪問管理後台: http://localhost:5000/admin?secret=YOUR_ADMIN_SECRET")
        print("2. 查看折線圖展示最近 7 天的任務趨勢")
        print("3. 查看圓環圖展示儲存空間使用情況")
        print("4. 重新整理頁面以查看實時數據")


if __name__ == '__main__':
    try:
        seed_database()
    except KeyboardInterrupt:
        print("\n\n⚠️  操作已取消")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 錯誤：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
