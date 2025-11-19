import os
import zipfile
import tarfile
import py7zr
import time
import shutil
import re
import threading
import hashlib
import base64
import atexit
import signal
from flask import Flask, request, jsonify, render_template, send_file
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import logging
from werkzeug.utils import secure_filename
from gridfs import GridFS
from urllib.parse import quote
import qrcode
import io
import secrets
import smtplib
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 設定 ---
UPLOAD_FOLDER = '/tmp/compressor_uploads'
OUTPUT_FOLDER = '/tmp/compressor_outputs'
for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- 資料庫與環境變數 ---
MONGO_URI = os.environ.get('MONGO_URI')
ADMIN_SECRET = os.environ.get('ADMIN_SECRET')
MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')

# --- 執行緒池與任務限制 ---
MAX_CONCURRENT_TASKS = int(os.environ.get('MAX_CONCURRENT_TASKS', 3))
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)
active_task_count = 0
task_lock = threading.Lock()

# --- 檔案驗證設定 ---
ALLOWED_EXTENSIONS = {'.zip', '.7z', '.gz', '.bz2', '.xz', '.tar'}
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', 100))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_DECOMPRESS_SIZE_BYTES = 1 * 1024 * 1024 * 1024 # 1 GB

# --- 壓縮參數限制 ---
MAX_ITERATIONS = 50  # 最多 50 層壓縮
MIN_ITERATIONS = 1   # 至少 1 層
MIN_MASTER_PASS_INTERVAL = 1  # 特殊密碼間隔至少 1 層

# --- 其他常數 ---
CANCEL_CHECK_INTERVAL = 5  # 每 5 層檢查一次取消狀態，平衡性能與響應速度
FILENAME_UNIQUE_SUFFIX_BYTES = 2  # 檔名唯一性後綴長度（2 bytes = 4 個字符）
TASK_SALT_BYTES = 16  # 任務鹽值長度（16 bytes = 32 個字符）
DELETE_TOKEN_BYTES = 16  # 刪除令牌長度（16 bytes = 32 個字符）
RANDOM_FILENAME_BYTES = 8  # 隨機檔名長度（8 bytes = 16 個字符）

client = None; db = None; tasks_collection = None; fs = None
try:
    if not MONGO_URI: raise ValueError("錯誤：找不到 MONGO_URI 環境變數。")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logging.info("✅ 成功連線至 MongoDB！")
    db = client['compressor_db']
    tasks_collection = db['tasks']
    fs = GridFS(db)
except Exception as e:
    logging.error(f"❌ 應用程式啟動失敗: {e}")

# --- 清理函數 ---
def cleanup_on_exit():
    """應用程式退出時清理資源"""
    logging.info("正在清理資源...")
    try:
        # 等待所有任務完成（最多等待 30 秒）
        executor.shutdown(wait=True, timeout=30)
        logging.info("✅ 線程池已清理")
    except Exception as e:
        logging.error(f"清理線程池時發生錯誤: {e}")

    try:
        # 關閉 MongoDB 連接
        if client:
            client.close()
            logging.info("✅ MongoDB 連接已關閉")
    except Exception as e:
        logging.error(f"關閉 MongoDB 連接時發生錯誤: {e}")

# 註冊清理函數
atexit.register(cleanup_on_exit)

# 處理信號（Ctrl+C 或 kill）
def signal_handler(signum, frame):
    logging.info(f"收到信號 {signum}，正在優雅地關閉...")
    cleanup_on_exit()
    exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- 通用輔助函式 ---
def generate_password(filename, salt, length=16):
    """
    使用檔案名稱和鹽通過 SHA-256 生成確定性密碼

    Args:
        filename: 檔案名稱
        salt: 任務唯一的鹽值
        length: 密碼長度（預設16）

    Returns:
        Base64 編碼的密碼字串
    """
    # 將檔案名稱和鹽組合
    data = f"{filename}:{salt}".encode('utf-8')
    # 使用 SHA-256 生成雜湊
    hash_digest = hashlib.sha256(data).digest()
    # 使用 Base64 編碼並移除特殊字符，只保留字母數字
    password = base64.urlsafe_b64encode(hash_digest).decode('utf-8')
    # 移除 padding 符號並截取指定長度
    password = password.replace('=', '').replace('-', '').replace('_', '')
    return password[:length]

def safe_db_operation(operation_func, operation_name="資料庫操作"):
    """
    安全執行資料庫操作的包裝函數
    Args:
        operation_func: 要執行的資料庫操作函數
        operation_name: 操作名稱（用於日誌）
    Returns:
        操作結果，如果失敗則返回 None
    """
    try:
        return operation_func()
    except Exception as e:
        logging.error(f"{operation_name}失敗: {e}", exc_info=True)
        return None

def update_task_log(task_id, message, is_progress_text=False):
    def _update():
        update_doc = {'$push': {'logs': message}}
        if is_progress_text:
            update_doc['$set'] = {'progress_text': message}
        return tasks_collection.update_one({'_id': task_id}, update_doc)
    safe_db_operation(_update, f"更新任務日誌 ({task_id})")

def update_task_progress(task_id, progress):
    def _update():
        return tasks_collection.update_one({'_id': task_id}, {'$set': {'progress': progress}})
    safe_db_operation(_update, f"更新任務進度 ({task_id})")
def parse_password_text(password_text):
    password_list = []
    for line in password_text.strip().split('\n'):
        match = re.search(r'第 \d+ 層 \((.*?)\):\s*(.*)', line)
        if match:
            fname, password = match.groups()
            password = password.strip()
            if password == '(特殊密碼層)':
                password_list.append({'filename': fname.strip(), 'password': 'MASTER_PASSWORD_PLACEHOLDER'})
            else:
                password_list.append({'filename': fname.strip(), 'password': None if password == '(無密碼)' else password})
    return password_list

def validate_compression_params(params):
    """
    驗證壓縮參數的合法性
    Raises:
        ValueError: 參數不合法時拋出異常
    """
    # 驗證 iterations
    iterations = params.get('iterations', 0)
    if not isinstance(iterations, int) or iterations < MIN_ITERATIONS or iterations > MAX_ITERATIONS:
        raise ValueError(f"壓縮層數必須在 {MIN_ITERATIONS} 到 {MAX_ITERATIONS} 之間。")

    # 驗證 master_pass_interval
    if params.get('use_master_pass'):
        interval = params.get('master_pass_interval', 0)
        if not isinstance(interval, int) or interval < MIN_MASTER_PASS_INTERVAL:
            raise ValueError(f"特殊密碼間隔必須至少為 {MIN_MASTER_PASS_INTERVAL} 層。")
        if interval > iterations:
            raise ValueError("特殊密碼間隔不能大於總壓縮層數。")

    # 驗證 manual_layers
    manual_layers = params.get('manual_layers', [])
    for layer in manual_layers:
        if not isinstance(layer, int) or layer < 1 or layer > iterations:
            raise ValueError(f"手動設定的密碼層 {layer} 超出有效範圍（1-{iterations}）。")

    # 驗證 formats
    if not params.get('formats') or len(params['formats']) == 0:
        raise ValueError("至少需要選擇一種壓縮格式。")

def validate_file(file, mode='compress'):
    if not file or not file.filename:
        raise ValueError("沒有選擇檔案或檔案名稱不可為空。")
    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    if file_length > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"檔案大小超過 {MAX_FILE_SIZE_MB}MB 的上限。")
    
    if mode == 'decompress':
        filename_lower = file.filename.lower()
        is_allowed_ext = any(filename_lower.endswith(ext) for ext in ALLOWED_EXTENSIONS)
        if not is_allowed_ext:
            raise ValueError(f"不支援的檔案格式: {filename_lower}")

        # 讀取更多 bytes 以支援 tar 格式驗證
        header = file.read(512)  # tar header 是 512 bytes
        file.seek(0)

        # ZIP 格式驗證
        zip_magic_numbers = [b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08']
        if filename_lower.endswith('.zip') and not any(header.startswith(sig) for sig in zip_magic_numbers):
            raise ValueError("檔案宣稱是 ZIP 檔，但內容格式不符，可能為惡意檔案。")

        # 7Z 格式驗證
        if filename_lower.endswith('.7z') and not header.startswith(b"7z\xbc\xaf'\x1c"):
            raise ValueError("檔案宣稱是 7z 檔，但內容格式不符，可能為惡意檔案。")

        # TAR 格式驗證（檢查 magic number "ustar" 在偏移量 257）
        if filename_lower.endswith('.tar') or filename_lower.endswith(('.gz', '.bz2', '.xz')):
            # .tar 檔案在偏移量 257 處有 "ustar" 標記
            # .gz 檔案以 \x1f\x8b 開頭（gzip magic number）
            # .bz2 檔案以 "BZ" 開頭
            # .xz 檔案以 \xfd\x37\x7a\x58\x5a\x00 開頭
            if filename_lower.endswith('.gz') and not header.startswith(b'\x1f\x8b'):
                raise ValueError("檔案宣稱是 GZIP 檔，但內容格式不符，可能為惡意檔案。")
            if filename_lower.endswith('.bz2') and not header.startswith(b'BZ'):
                raise ValueError("檔案宣稱是 BZIP2 檔，但內容格式不符，可能為惡意檔案。")
            if filename_lower.endswith('.xz') and not header.startswith(b'\xfd\x37\x7a\x58\x5a\x00'):
                raise ValueError("檔案宣稱是 XZ 檔，但內容格式不符，可能為惡意檔案。")
            if filename_lower.endswith('.tar') and len(header) >= 262:
                # TAR 檔案的 magic number 在偏移量 257
                tar_magic = header[257:262]
                if tar_magic not in [b'ustar', b'ustar\x00']:
                    raise ValueError("檔案宣稱是 TAR 檔，但內容格式不符，可能為惡意檔案。")

# --- 背景任務 ---
def task_wrapper(func, *args, **kwargs):
    global active_task_count
    with task_lock:
        active_task_count += 1
    try:
        func(*args, **kwargs)
    finally:
        with task_lock:
            active_task_count -= 1
        
def compression_worker(task_id_str, recipient_email=None, host_url=None):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']
    # 記錄原始文件大小用於壓縮率計算
    original_size = os.path.getsize(original_file)
    # 追蹤所有生成的檔案以便失敗時清理
    generated_files = []
    try:
        iterations = params['iterations']
        password_file_content = "--- 壓縮密碼表 ---\n"
        # 為本次任務生成唯一的鹽（基於 task_id 和時間戳）
        task_salt = secrets.token_hex(TASK_SALT_BYTES)
        password_file_content += f"# 任務鹽值 (Salt): {task_salt}\n"
        formats = {'zip':'.zip', '7z':'.7z', 'targz':'.tar.gz'}
        current_file = original_file
        for i in range(1, iterations + 1):
            # 每隔幾層檢查一次取消狀態，避免頻繁查詢資料庫
            if i % CANCEL_CHECK_INTERVAL == 0 or i == 1:
                task_status = safe_db_operation(
                    lambda: tasks_collection.find_one({'_id': task_id}, {'cancel_requested': 1}),
                    "檢查取消狀態"
                )
                if task_status and task_status.get('cancel_requested'):
                    update_task_log(task_id, "⚠️ 日誌: 操作已被使用者取消。")
                    safe_db_operation(
                        lambda: tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '已取消', 'progress_text': '已取消'}}),
                        "設定取消狀態"
                    )
                    # 清理已生成的中間檔案
                    for temp_file in generated_files:
                        if os.path.exists(temp_file):
                            try:
                                os.remove(temp_file)
                                logging.info(f"已清理取消任務的中間檔案: {temp_file}")
                            except Exception as e:
                                logging.error(f"清理中間檔案失敗: {e}")
                    return
            format_name = params['formats'][(i - 1) % len(params['formats'])]

            # 最後一層使用原始檔名，其他層使用隨機檔名
            if i == iterations:
                # 最後一層：使用原始檔名（不含原副檔名）+ 短隨機碼 + 新格式副檔名
                # 使用 secure_filename 清理檔名，防止路徑穿越
                safe_raw_filename = secure_filename(params['raw_filename'])
                base_name = os.path.splitext(safe_raw_filename)[0]
                # 加上隨機碼避免檔名衝突
                unique_suffix = secrets.token_hex(FILENAME_UNIQUE_SUFFIX_BYTES)
                final_filename = f"{base_name}_{unique_suffix}{formats[format_name]}"
                output_filename = os.path.join(OUTPUT_FOLDER, final_filename)
                filename_for_password = final_filename
            else:
                # 中間層：使用隨機檔名
                random_filename = secrets.token_hex(RANDOM_FILENAME_BYTES) + formats[format_name]
                output_filename = os.path.join(OUTPUT_FOLDER, random_filename)
                filename_for_password = random_filename

            password = None; log_pwd = "(無密碼)"
            if params['use_master_pass'] and i % params['master_pass_interval'] == 0:
                password = params['master_pass']; log_pwd = "(特殊密碼層)"
            elif (params['encrypt_odd'] and i % 2 != 0) or (not params['encrypt_odd'] and i in params['manual_layers']):
                if format_name in ('zip', '7z'):
                    # 使用檔名和任務鹽生成 SHA-256 密碼
                    password = generate_password(filename_for_password, task_salt)
                    log_pwd = password
            password_file_content += f"第 {i} 層 ({os.path.basename(output_filename)}): {log_pwd}\n"
            progress_text = f"正在壓縮第 {i}/{iterations} 層 (格式: {format_name})"
            update_task_log(task_id, f"--- {progress_text} ---", is_progress_text=True)

            # 執行壓縮
            if format_name in ('zip', '7z'):
                with py7zr.SevenZipFile(output_filename, 'w', password=password) as z:
                    z.write(current_file, os.path.basename(current_file))
            else:
                with tarfile.open(output_filename, 'w:gz') as tf:
                    tf.add(current_file, arcname=os.path.basename(current_file))

            # 追蹤生成的檔案
            generated_files.append(output_filename)
            if current_file != original_file: os.remove(current_file)
            current_file = output_filename
            update_task_progress(task_id, int((i / iterations) * 100))
        update_task_log(task_id, "✅ 壓縮流程結束。", is_progress_text=True)
        # 計算壓縮率
        final_size = os.path.getsize(current_file)
        compression_ratio = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0

        with open(current_file, 'rb') as f_in:
            file_id = fs.put(f_in, filename=os.path.basename(current_file))
        os.remove(current_file)
        delete_token = secrets.token_hex(DELETE_TOKEN_BYTES)
        tasks_collection.update_one({'_id': task_id}, {'$set': {
            'status': '完成', 'progress': 100,
            'result_file_id': str(file_id), 'result_filename': os.path.basename(current_file),
            'password_file_content': password_file_content, 'delete_token': delete_token,
            'original_size': original_size, 'final_size': final_size, 'compression_ratio': round(compression_ratio, 2)
        }})
        update_task_log(task_id, f"📊 壓縮率: {compression_ratio:.2f}% (原始: {original_size/1024:.2f} KB → 壓縮後: {final_size/1024:.2f} KB)")
        if recipient_email and host_url:
            try:
                send_completion_email(recipient_email, task_id_str, params['raw_filename'], host_url)
                update_task_log(task_id, f"✅ 已成功寄送通知信至: {recipient_email}")
            except Exception as e:
                update_task_log(task_id, f"⚠️ 寄送通知信失敗: {e}")
    except (py7zr.Bad7zFile, zipfile.BadZipFile, tarfile.ReadError) as e:
        update_task_log(task_id, f"❌ 檔案格式錯誤或已損毀: {e}")
        safe_db_operation(
            lambda: tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}}),
            "設定任務失敗狀態"
        )
    except Exception as e:
        logging.error(f"壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        safe_db_operation(
            lambda: tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}}),
            "設定任務失敗狀態"
        )
    finally:
        # 清理原始上傳檔案
        if 'original_file' in locals() and os.path.exists(original_file):
            os.remove(original_file)
        # 如果任務失敗，清理所有生成的中間檔案
        if 'generated_files' in locals():
            task_status = safe_db_operation(
                lambda: tasks_collection.find_one({'_id': task_id}, {'status': 1}),
                "查詢任務狀態"
            )
            if task_status and task_status.get('status') == '失敗':
                for temp_file in generated_files:
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                            logging.info(f"已清理失敗任務的檔案: {temp_file}")
                        except Exception as cleanup_err:
                            logging.error(f"清理檔案失敗: {cleanup_err}")

def decompression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_decompress_temp")
    # 用於檢查取消狀態的標誌
    cancel_check_interval = 5
    # 追蹤已處理的中間檔案
    processed_files = []
    try:
        password_list = params['password_list']
        master_pass = params.get('master_pass')
        if not password_list: raise ValueError("找不到可用的密碼表。")
        current_file = original_file; total_layers = len(password_list)
        total_uncompressed_size = 0
        for i, layer_info in enumerate(reversed(password_list)):
            # 優化取消檢查頻率
            if i % cancel_check_interval == 0 or i == 0:
                task_status = safe_db_operation(
                    lambda: tasks_collection.find_one({'_id': task_id}, {'cancel_requested': 1}),
                    "檢查取消狀態"
                )
                if task_status and task_status.get('cancel_requested'):
                    update_task_log(task_id, "⚠️ 日誌: 操作已被使用者取消。")
                    safe_db_operation(
                        lambda: tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '已取消', 'progress_text': '已取消'}}),
                        "設定取消狀態"
                    )
                    # 清理已處理的中間檔案
                    for temp_file in processed_files:
                        if os.path.exists(temp_file):
                            try:
                                os.remove(temp_file)
                                logging.info(f"已清理取消任務的中間檔案: {temp_file}")
                            except Exception as e:
                                logging.error(f"清理中間檔案失敗: {e}")
                    return
            layer_num = total_layers - i
            password = layer_info['password']
            if password == 'MASTER_PASSWORD_PLACEHOLDER':
                if not master_pass: raise ValueError(f"第 {layer_num} 層需要特殊密碼。")
                password = master_pass
            
            progress_text = f"正在解壓縮第 {layer_num}/{total_layers} 層"
            update_task_log(task_id, f"--- {progress_text} ---", is_progress_text=True)
            
            os.makedirs(output_path, exist_ok=True)
            if layer_info['filename'].endswith(('.zip', '.7z')):
                with py7zr.SevenZipFile(current_file, 'r', password=password) as z:
                    z.extractall(path=output_path)
            else:
                with tarfile.open(current_file, 'r:*') as tf:
                    tf.extractall(path=output_path)
            
            current_layer_size = sum(os.path.getsize(os.path.join(root, name)) for root, _, files in os.walk(output_path) for name in files)
            total_uncompressed_size += current_layer_size
            if total_uncompressed_size > MAX_DECOMPRESS_SIZE_BYTES:
                raise Exception(f"解壓縮後的檔案總大小超過 1GB 上限，為防止 Zip Bomb 攻擊，已中止操作。")

            if current_file != original_file: os.remove(current_file)
            extracted_items = os.listdir(output_path)
            if not extracted_items: raise Exception("解壓縮後找不到任何檔案。")

            # 防止路徑穿越攻擊：使用 basename 清理檔案名稱
            safe_item_name = os.path.basename(extracted_items[0])
            next_item_path = os.path.join(output_path, extracted_items[0])
            moved_item_path = os.path.join(OUTPUT_FOLDER, safe_item_name)

            # 確保目標路徑在 OUTPUT_FOLDER 內
            if not os.path.abspath(moved_item_path).startswith(os.path.abspath(OUTPUT_FOLDER)):
                raise Exception("偵測到路徑穿越攻擊，已中止操作。")

            shutil.move(next_item_path, moved_item_path)
            shutil.rmtree(output_path)
            current_file = moved_item_path
            # 追蹤處理的中間檔案
            if current_file != original_file:
                processed_files.append(current_file)
            update_task_progress(task_id, int(((i + 1) / total_layers) * 100))

        update_task_log(task_id, "日誌: 所有層級已解壓，正在檢查最終內容...", is_progress_text=True)
        expected_filename = params.get('expected_filename', 'decompressed_output.zip')
        
        if os.path.isdir(current_file):
            update_task_log(task_id, "日誌: 偵測到多個檔案，將打包成 ZIP 檔。")
            final_zip_name_base = os.path.splitext(expected_filename)[0]
            final_filename_to_store = f"{final_zip_name_base}.zip"
            final_archive_path = os.path.join(OUTPUT_FOLDER, final_filename_to_store)

            with zipfile.ZipFile(final_archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, _, files in os.walk(current_file):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, current_file)
                        zipf.write(file_path, arcname)
            file_to_upload = final_archive_path
        else:
            update_task_log(task_id, "日誌: 偵測到單一檔案，將保留原始檔名。")
            final_filename_to_store = expected_filename
            file_to_upload = current_file

        with open(file_to_upload, 'rb') as f_in:
            file_id = fs.put(f_in, filename=final_filename_to_store)

        tasks_collection.update_one({'_id': task_id}, {'$set': {
            'status': '完成', 'progress': 100, 
            'result_file_id': str(file_id), 'result_filename': final_filename_to_store,
            'progress_text': '任務完成！'
        }})
        update_task_log(task_id, "✅ 解壓縮流程結束。")
    except (py7zr.Bad7zFile, zipfile.BadZipFile, tarfile.ReadError) as e:
        update_task_log(task_id, f"❌ 檔案格式錯誤或已損毀: {e}")
        safe_db_operation(
            lambda: tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}}),
            "設定任務失敗狀態"
        )
    except Exception as e:
        logging.error(f"解壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        safe_db_operation(
            lambda: tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}}),
            "設定任務失敗狀態"
        )
    finally:
        # 清理原始上傳檔案
        if 'original_file' in locals() and os.path.exists(original_file):
            os.remove(original_file)

        # 清理臨時解壓目錄
        if 'output_path' in locals() and os.path.exists(output_path):
            try:
                shutil.rmtree(output_path)
                logging.info(f"已清理臨時解壓目錄: {output_path}")
            except Exception as e:
                logging.error(f"清理臨時目錄失敗: {e}")

        # 如果任務失敗，清理所有中間檔案
        task_status = safe_db_operation(
            lambda: tasks_collection.find_one({'_id': task_id}, {'status': 1}),
            "查詢任務狀態"
        )
        if task_status and task_status.get('status') == '失敗':
            if 'current_file' in locals() and os.path.exists(current_file):
                try:
                    if os.path.isdir(current_file):
                        shutil.rmtree(current_file)
                    else:
                        os.remove(current_file)
                    logging.info(f"已清理失敗任務的檔案: {current_file}")
                except Exception as e:
                    logging.error(f"清理檔案失敗: {e}")

            if 'final_archive_path' in locals() and os.path.exists(final_archive_path):
                try:
                    os.remove(final_archive_path)
                    logging.info(f"已清理失敗任務的歸檔: {final_archive_path}")
                except Exception as e:
                    logging.error(f"清理歸檔失敗: {e}")

def send_completion_email(recipient_email, task_id, original_filename, host_url):
    if not MAIL_USERNAME or not MAIL_PASSWORD: raise Exception("伺服器未設定郵件功能。")
    msg = EmailMessage()
    msg['Subject'] = f"您的檔案「{original_filename}」已壓縮完成！"
    msg['From'] = MAIL_USERNAME
    msg['To'] = recipient_email
    download_url = f"{host_url}download/{task_id}"
    share_url = f"{host_url}?share_id={task_id}"
    html_content = f"<html><body><p>您好，</p><p>您先前提交的檔案 <b>{original_filename}</b> 已經成功壓縮完成了。</p><p>您可以透過以下連結進行操作：</p><ul><li><a href='{download_url}'><b>直接下載壓縮檔</b></a></li><li><a href='{share_url}'>產生分享連結與 QR Code</a></li></ul><p>感謝您的使用！</p></body></html>"
    msg.add_alternative(html_content, subtype='html')
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
        smtp.send_message(msg)

# --- 安全標頭設置 ---
@app.after_request
def set_security_headers(response):
    """為所有回應添加安全標頭"""
    # 防止 XSS 攻擊
    response.headers['X-Content-Type-Options'] = 'nosniff'
    # 防止點擊劫持
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    # 內容安全政策（允許 Tailwind CSS CDN）
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; img-src 'self' data:; font-src 'self' https://cdn.jsdelivr.net"
    # 強制 HTTPS（在生產環境中啟用）
    # response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# --- API 路由 ---
def handle_route_exception(e, endpoint_name):
    logging.error(f"路由 {endpoint_name} 發生錯誤: {e}", exc_info=True)
    if isinstance(e, ValueError):
        return jsonify({'error': str(e)}), 400
    return jsonify({'error': '伺服器內部發生錯誤，請稍後再試。'}), 500

@app.route('/')
def index(): return render_template('index.html')

@app.route('/compress', methods=['POST'])
def compress_route():
    if active_task_count >= MAX_CONCURRENT_TASKS:
        return jsonify({'error': '伺服器目前忙碌中，請稍後再試。'}), 429
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500

        # 調試：檢查請求內容
        logging.info(f"=== 接收到壓縮請求 ===")
        logging.info(f"Content-Type: {request.content_type}")
        logging.info(f"Content-Length: {request.headers.get('Content-Length', 'N/A')}")
        logging.info(f"Transfer-Encoding: {request.headers.get('Transfer-Encoding', 'N/A')}")
        logging.info(f"X-Forwarded-For: {request.headers.get('X-Forwarded-For', 'N/A')}")

        # 檢查原始數據是否存在
        try:
            # 先偷看一下原始數據的開頭（不消耗 stream）
            if hasattr(request, 'stream') and request.stream:
                logging.info(f"request.stream 存在")

            # 檢查 environ 中的 wsgi.input
            if 'wsgi.input' in request.environ:
                logging.info(f"wsgi.input 存在於 environ 中")
                wsgi_input = request.environ['wsgi.input']
                logging.info(f"wsgi.input 類型: {type(wsgi_input)}")
        except Exception as e:
            logging.warning(f"檢查原始數據時發生錯誤: {e}")

        logging.info(f"開始解析 multipart 數據...")
        try:
            # 先看看 request.files 和 request.form 裡有什麼
            logging.info(f"request.files keys: {list(request.files.keys())}")
            logging.info(f"request.form keys: {list(request.form.keys())}")

            # 如果 files 裡有東西，列出所有 keys
            if request.files:
                for key in request.files.keys():
                    logging.info(f"  - files['{key}']: {request.files[key]}")

            file = request.files.get('file')
            logging.info(f"✅ 成功獲取檔案物件: {file}")
            if file:
                logging.info(f"檔案名稱: {file.filename}, 大小: {request.content_length} bytes")
        except Exception as parse_error:
            logging.error(f"❌ 解析 multipart 時發生錯誤: {parse_error}", exc_info=True)
            raise ValueError(f"無法解析上傳的檔案數據: {str(parse_error)}")

        validate_file(file, mode='compress')

        # 取得來源 IP 位址
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)

        # 解析並驗證參數
        try:
            iterations = int(request.form.get('iterations', 5))
            master_pass_interval = int(request.form.get('master_password_interval', '10'))
            manual_layers = [int(x.strip()) for x in request.form.get('manual_layers', '').split(',') if x.strip()]
        except ValueError:
            raise ValueError("參數格式錯誤，請檢查數字欄位。")

        params = {
            'raw_filename': file.filename,
            'expected_filename': file.filename,
            'iterations': iterations,
            'encrypt_odd': request.form.get('encrypt_mode', 'odd') == 'odd',
            'manual_layers': manual_layers,
            'formats': [x.strip() for x in request.form.get('formats', 'zip,7z,targz').split(',') if x.strip()],
            'use_master_pass': request.form.get('use_master_pass') == 'on',
            'master_pass': request.form.get('master_password'),
            'master_pass_interval': master_pass_interval
        }

        # 驗證壓縮參數
        validate_compression_params(params)

        task = {'type': 'compress', 'status': 'pending', 'params': params, 'created_at': datetime.utcnow(), 'ip_address': ip_address}
        task_id = tasks_collection.insert_one(task).inserted_id

        # 使用 try-except 包裹檔案上傳，失敗時清理資料庫記錄
        try:
            filepath = os.path.join(UPLOAD_FOLDER, f"{str(task_id)}_{secure_filename(file.filename)}")
            file.save(filepath)
            tasks_collection.update_one({'_id': task_id}, {'$set': {'params.original_file': filepath, 'status': '處理中', 'progress_text': '準備開始...'}})
            executor.submit(task_wrapper, compression_worker, str(task_id), request.form.get('recipient_email'), request.host_url)
            return jsonify({'task_id': str(task_id)})
        except Exception as upload_err:
            # 上傳失敗，清理資料庫記錄
            safe_db_operation(lambda: tasks_collection.delete_one({'_id': task_id}), "清理失敗的任務記錄")
            # 如果檔案已部分寫入，嘗試刪除
            if 'filepath' in locals() and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            raise ValueError(f"檔案上傳失敗: {upload_err}")
    except Exception as e:
        return handle_route_exception(e, 'compress')

@app.route('/decompress-manual', methods=['POST'])
def decompress_manual_route():
    if active_task_count >= MAX_CONCURRENT_TASKS:
        return jsonify({'error': '伺服器目前忙碌中，請稍後再試。'}), 429
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        file = request.files.get('file')
        validate_file(file, mode='decompress')
        
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)

        params = { 
            'password_list': parse_password_text(request.form.get('passwords', '')), 
            'master_pass': request.form.get('master_password'), 
            'expected_filename': file.filename 
        }
        if not params['password_list']: raise ValueError("無法解析您提供的密碼表。")
        
        task = {
            'type': 'decompress',
            'status': 'pending',
            'params': params,
            'created_at': datetime.utcnow(),
            'ip_address': ip_address
        }
        task_id = tasks_collection.insert_one(task).inserted_id

        # 使用 try-except 包裹檔案上傳，失敗時清理資料庫記錄
        try:
            filepath = os.path.join(UPLOAD_FOLDER, f"{str(task_id)}_{secure_filename(file.filename)}")
            file.save(filepath)
            tasks_collection.update_one({'_id': task_id}, {'$set': {'params.original_file': filepath, 'status': '處理中', 'progress_text': '準備開始...'}})
            executor.submit(task_wrapper, decompression_worker, str(task_id))
            return jsonify({'task_id': str(task_id)})
        except Exception as upload_err:
            # 上傳失敗，清理資料庫記錄
            safe_db_operation(lambda: tasks_collection.delete_one({'_id': task_id}), "清理失敗的任務記錄")
            # 如果檔案已部分寫入，嘗試刪除
            if 'filepath' in locals() and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            raise ValueError(f"檔案上傳失敗: {upload_err}")
    except Exception as e:
        return handle_route_exception(e, 'decompress_manual')

@app.route('/start-shared-decompression/<compress_task_id>', methods=['POST'])
def start_shared_decompression(compress_task_id):
    if active_task_count >= MAX_CONCURRENT_TASKS:
        return jsonify({'error': '伺服器目前忙碌中，請稍後再試。'}), 429
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        original_task = tasks_collection.find_one({'_id': ObjectId(compress_task_id)})
        if not original_task or 'result_file_id' not in original_task: raise ValueError("找不到原始壓縮任務或檔案可能已被刪除。")
        
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)

        filepath = os.path.join(UPLOAD_FOLDER, f"share_{secure_filename(original_task['result_filename'])}")
        grid_out = fs.get(ObjectId(original_task['result_file_id']))
        with open(filepath, 'wb') as f_out:
            for chunk in grid_out:
                f_out.write(chunk)
        params = {
            'original_file': filepath, 'password_list': parse_password_text(original_task.get('password_file_content', '')),
            'master_pass': request.get_json().get('master_password'), 'expected_filename': original_task.get('params', {}).get('raw_filename')
        }
        new_task = {
            'type': 'decompress', 
            'status': '處理中', 
            'params': params, 
            'created_at': datetime.utcnow(),
            'ip_address': ip_address
        }
        new_task_id = tasks_collection.insert_one(new_task).inserted_id
        tasks_collection.update_one({'_id': new_task_id}, {'$set': {'progress_text': '準備開始...'}})
        executor.submit(task_wrapper, decompression_worker, str(new_task_id))
        return jsonify({'task_id': str(new_task_id)})
    except Exception as e:
        return handle_route_exception(e, 'start_shared_decompression')

@app.route('/admin')
def admin_dashboard():
    return render_template('admin.html')

@app.route('/admin/api/decompression-logs')
def get_decompression_logs():
    try:
        if not ADMIN_SECRET:
            return jsonify({'error': '伺服器未設定管理員密碼'}), 500

        provided_secret = request.args.get('secret', '')
        if not provided_secret:
            return jsonify({'error': '缺少管理員密碼'}), 401

        # 使用 secrets.compare_digest 防止時序攻擊
        # 確保兩個參數都是字串類型
        try:
            if not secrets.compare_digest(str(provided_secret), str(ADMIN_SECRET)):
                return jsonify({'error': '管理員密碼錯誤'}), 403
        except Exception:
            return jsonify({'error': '管理員密碼錯誤'}), 403

        pipeline = [
            {'$match': {'type': 'decompress', 'status': '完成', 'ip_address': {'$exists': True}}},
            {'$sort': {'created_at': -1}},
            {'$group': {
                '_id': '$ip_address',
                'count': {'$sum': 1},
                'files': {
                    '$push': {
                        'filename': '$result_filename',
                        'original_filename': '$params.expected_filename',
                        'timestamp': '$created_at'
                    }
                },
                'last_activity': {'$first': '$created_at'}
            }},
            {'$sort': {'last_activity': -1}},
            {'$project': {
                'ip_address': '$_id',
                'count': 1,
                'files': 1,
                'last_activity': 1,
                '_id': 0
            }}
        ]
        logs = list(tasks_collection.aggregate(pipeline))
        return jsonify(logs)

    except Exception as e:
        return handle_route_exception(e, 'get_decompression_logs')

@app.route('/health')
def health_check():
    health = {'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()}
    status_code = 200
    try:
        client.admin.command('ping')
        health['database'] = 'connected'
    except Exception as e:
        health['status'] = 'degraded'; health['database'] = f'disconnected: {str(e)}'; status_code = 503
    try:
        disk = shutil.disk_usage('/')
        health['disk_space'] = {'total_gb': disk.total // (2**30), 'free_gb': disk.free // (2**30)}
        if (disk.free / disk.total) < 0.1:
             health['status'] = 'degraded'; health['disk_space']['warning'] = 'Low disk space'; status_code = 503
    except Exception as e:
        health['status'] = 'degraded'; health['disk_space'] = f'Error: {str(e)}'; status_code = 503
    return jsonify(health), status_code
    
@app.route('/storage-stats')
def storage_stats():
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500

        # 計算總使用空間
        pipeline = [{'$group': {'_id': None, 'total_size': {'$sum': '$length'}}}]
        result = list(db['fs.files'].aggregate(pipeline))
        used_space_bytes = result[0]['total_size'] if result else 0

        # 計算檔案數量
        file_count = db['fs.files'].count_documents({})

        # MongoDB 免費版限制
        total_space_bytes = 512 * 1024 * 1024  # 512 MB

        # 計算使用百分比
        usage_percent = round((used_space_bytes / total_space_bytes) * 100, 2)

        # 可用空間
        available_bytes = total_space_bytes - used_space_bytes

        # 判斷警告等級
        warning_level = 'normal'  # normal / warning / danger / full
        can_upload = True

        if usage_percent >= 100:
            warning_level = 'full'
            can_upload = False
        elif usage_percent >= 95:
            warning_level = 'danger'
        elif usage_percent >= 80:
            warning_level = 'warning'

        return jsonify({
            'used_space_bytes': used_space_bytes,
            'used_space_mb': round(used_space_bytes / (1024 * 1024), 2),
            'total_space_bytes': total_space_bytes,
            'total_space_mb': 512,
            'available_bytes': available_bytes,
            'available_mb': round(available_bytes / (1024 * 1024), 2),
            'usage_percent': usage_percent,
            'file_count': file_count,
            'warning_level': warning_level,
            'can_upload': can_upload
        })
    except Exception as e:
        return handle_route_exception(e, 'storage_stats')

@app.route('/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    try:
        tasks_collection.update_one({'_id': ObjectId(task_id)}, {'$set': {'cancel_requested': True}})
        return jsonify({'status': 'cancellation requested'})
    except Exception as e:
        return handle_route_exception(e, 'cancel')

@app.route('/status/<task_id>')
def task_status(task_id):
    try:
        task = tasks_collection.find_one({'_id': ObjectId(task_id)})
        if task:
            task['_id'] = str(task['_id']); return jsonify(task)
        return jsonify({'error': '找不到任務'}), 404
    except Exception as e:
        return handle_route_exception(e, 'status')

@app.route('/delete/<task_id>', methods=['POST'])
def delete_file(task_id):
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        token = request.get_json().get('token')
        if not token: return jsonify({'error': '缺少 Token'}), 400
        task = tasks_collection.find_one({'_id': ObjectId(task_id)})
        if not task: return jsonify({'error': '找不到任務'}), 404
        if not secrets.compare_digest(task.get('delete_token', ""), token): return jsonify({'error': 'Token 無效'}), 403
        if 'result_file_id' in task and task['result_file_id']:
            fs.delete(ObjectId(task['result_file_id']))
        tasks_collection.update_one({'_id': ObjectId(task_id)}, {
            '$unset': { 'result_file_id': "", 'result_filename': "", 'password_file_content': "", 'delete_token': "" },
            '$set': {'status': '已刪除'}
        })
        return jsonify({'message': '檔案已成功刪除'})
    except Exception as e:
        return handle_route_exception(e, 'delete')

@app.route('/delete-batch', methods=['POST'])
def delete_batch():
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        tasks_to_delete = request.get_json().get('tasks', [])
        deleted_count = 0; failed_count = 0
        for task_info in tasks_to_delete:
            task_id = task_info.get('id'); token = task_info.get('token')
            if not task_id or not token:
                failed_count += 1; continue
            try:
                task = tasks_collection.find_one({'_id': ObjectId(task_id)})
                if task and secrets.compare_digest(task.get('delete_token', ""), token):
                    if 'result_file_id' in task and task['result_file_id']:
                        fs.delete(ObjectId(task['result_file_id']))
                    tasks_collection.update_one({'_id': ObjectId(task_id)}, {
                        '$unset': { 'result_file_id': "", 'result_filename': "", 'password_file_content': "", 'delete_token': "" },
                        '$set': {'status': '已刪除'}
                    })
                    deleted_count += 1
                else: failed_count += 1
            except Exception:
                failed_count += 1
        return jsonify({'message': '批次刪除處理完成', 'deleted_count': deleted_count, 'failed_count': failed_count})
    except Exception as e:
        return handle_route_exception(e, 'delete_batch')

@app.route('/delete-all-files', methods=['POST'])
def delete_all_files():
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        if not ADMIN_SECRET: return jsonify({'error': '伺服器未設定管理員密碼'}), 500
        admin_secret_provided = request.get_json().get('admin_secret', "")
        if not secrets.compare_digest(admin_secret_provided, ADMIN_SECRET):
            return jsonify({'error': '管理員密碼錯誤'}), 403
        all_files = list(db.fs.files.find({}))
        deleted_count = len(all_files)
        for file_doc in all_files:
            fs.delete(file_doc['_id'])
        tasks_collection.update_many(
            {'result_file_id': {'$exists': True}},
            {'$set': {'status': '已刪除 (管理員清除)'},
             '$unset': { 'result_file_id': "", 'result_filename': "", 'password_file_content': "", 'delete_token': "" }}
        )
        return jsonify({'message': '所有檔案已成功刪除', 'deleted_count': deleted_count})
    except Exception as e:
        return handle_route_exception(e, 'delete_all_files')

@app.route('/qrcode/<task_id>')
def generate_qr_code(task_id):
    try:
        share_url = f"{request.host_url}?share_id={task_id}"
        img_io = io.BytesIO()
        qrcode.make(share_url).save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')
    except Exception as e:
        return handle_route_exception(e, 'qrcode')

@app.route('/download/<task_id>')
def download_file(task_id):
    try:
        task = tasks_collection.find_one({'_id': ObjectId(task_id)})
        if not task or 'result_file_id' not in task:
            return "檔案可能已被刪除或不存在。", 404
        
        grid_out = fs.get(ObjectId(task['result_file_id']))
        response = send_file(grid_out, mimetype='application/octet-stream', as_attachment=True, download_name=task['result_filename'])
        encoded_filename = quote(task['result_filename'].encode('utf-8'))
        response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
        return response
    except Exception as e:
        return handle_route_exception(e, 'download')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

