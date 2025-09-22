import os
import zipfile
import tarfile
import py7zr
import time
import shutil
import string
import random
import re
import threading
import hashlib
import base64
from flask import Flask, request, jsonify, send_from_directory, render_template, send_file
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

# --- 關鍵修改：執行緒池與任務限制 ---
MAX_CONCURRENT_TASKS = int(os.environ.get('MAX_CONCURRENT_TASKS', 3))
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)
active_task_count = 0

# --- 關鍵修改：檔案驗證設定 ---
ALLOWED_EXTENSIONS = {'.zip', '.7z', '.gz', '.bz2', '.xz', '.tar'}
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', 100))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_DECOMPRESS_SIZE_BYTES = 1 * 1024 * 1024 * 1024 # 1 GB

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

# --- 通用輔助函式 ---
def generate_password(length=12):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))
def update_task_log(task_id, message, is_progress_text=False):
    update_doc = {'$push': {'logs': message}}
    if is_progress_text:
        update_doc['$set'] = {'progress_text': message}
    tasks_collection.update_one({'_id': task_id}, update_doc)
def update_task_progress(task_id, progress):
    tasks_collection.update_one({'_id': task_id}, {'$set': {'progress': progress}})
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

# --- 關鍵修改：檔案驗證輔助函式 ---
def validate_file(file, mode='compress'):
    if file.filename == '':
        raise ValueError("檔案名稱不可為空。")
    
    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    if file_length > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"檔案大小超過 {MAX_FILE_SIZE_MB}MB 的上限。")

    if mode == 'decompress':
        is_allowed = any(file.filename.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS)
        if not is_allowed:
            raise ValueError(f"不支援的檔案格式: {file.filename}")

# --- 背景任務 ---
def task_wrapper(func, *args, **kwargs):
    global active_task_count
    active_task_count += 1
    try:
        func(*args, **kwargs)
    finally:
        active_task_count -= 1
        
def compression_worker(task_id_str, recipient_email=None, host_url=None):
    task_id = ObjectId(task_id_str)
    try:
        # ... (壓縮的核心邏輯不變) ...
    except (py7zr.Bad7zFile, zipfile.BadZipFile, tarfile.ReadError) as e:
        update_task_log(task_id, f"❌ 檔案格式錯誤或已損毀: {e}")
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    except Exception as e:
        logging.error(f"壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    # ... (finally 區塊不變) ...

def decompression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_decompress_temp")
    
    try:
        # ... (解壓縮迴圈邏輯與之前類似) ...
        total_uncompressed_size = 0
        for i, layer_info in enumerate(reversed(params['password_list'])):
            # ...
            # *** 關鍵修改：Zip Bomb 防護 ***
            current_layer_size = 0
            for root, _, files in os.walk(output_path):
                for name in files:
                    current_layer_size += os.path.getsize(os.path.join(root, name))
            total_uncompressed_size += current_layer_size
            if total_uncompressed_size > MAX_DECOMPRESS_SIZE_BYTES:
                raise Exception(f"解壓縮後的檔案總大小超過 1GB 上限，為防止 Zip Bomb 攻擊，已中止操作。")
            # ...
        
        # *** 關鍵修改：智慧型拆包邏輯 ***
        # ... (此邏輯與之前版本相同) ...
    except (py7zr.Bad7zFile, zipfile.BadZipFile, tarfile.ReadError) as e:
        update_task_log(task_id, f"❌ 檔案格式錯誤或已損毀: {e}")
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    except Exception as e:
        logging.error(f"解壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    # ... (finally 區塊不變) ...

# ... (send_completion_email 函式不變) ...

# --- API 路由 ---
def handle_route_exception(e, endpoint_name):
    logging.error(f"路由 {endpoint_name} 發生錯誤: {e}", exc_info=True)
    # 針對使用者輸入的錯誤，回傳更具體的訊息
    if isinstance(e, ValueError):
        return jsonify({'error': str(e)}), 400
    return jsonify({'error': '伺服器內部發生錯誤，請稍後再試。'}), 500

@app.route('/')
def index(): return render_template('index.html')

@app.route('/compress', methods=['POST'])
def compress_route():
    global active_task_count
    if active_task_count >= MAX_CONCURRENT_TASKS:
        return jsonify({'error': '伺服器目前忙碌中，請稍後再試。'}), 429
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        if 'file' not in request.files: return jsonify({'error': '沒有上傳檔案'}), 400
        file = request.files['file']
        validate_file(file, mode='compress')
        params = {
            'raw_filename': file.filename, 'iterations': int(request.form.get('iterations', 5)),
            'encrypt_odd': request.form.get('encrypt_mode', 'odd') == 'odd',
            'manual_layers': [int(x.strip()) for x in request.form.get('manual_layers', '').split(',') if x.strip()],
            'formats': [x.strip() for x in request.form.get('formats', 'zip,7z,targz').split(',') if x.strip()],
            'use_master_pass': request.form.get('use_master_pass') == 'on',
            'master_pass': request.form.get('master_password'),
            'master_pass_interval': int(request.form.get('master_password_interval', '10'))
        }
        task = {'type': 'compress', 'status': 'pending', 'params': params, 'created_at': datetime.utcnow()}
        task_id = tasks_collection.insert_one(task).inserted_id
        filepath = os.path.join(UPLOAD_FOLDER, f"{str(task_id)}_{secure_filename(file.filename)}")
        file.save(filepath)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'params.original_file': filepath, 'status': '處理中', 'progress_text': '準備開始...'}})
        executor.submit(task_wrapper, compression_worker, str(task_id), request.form.get('recipient_email'), request.host_url)
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        return handle_route_exception(e, 'compress')
        
# ... (其餘路由，如 decompress-manual, start-shared-decompression 等也應加上 active_task_count 檢查與 validate_file)

# *** 關鍵修改：健康檢查路由 ***
@app.route('/health')
def health_check():
    health = {'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()}
    status_code = 200
    try:
        client.admin.command('ping')
        health['database'] = 'connected'
    except Exception as e:
        health['status'] = 'degraded'
        health['database'] = f'disconnected: {str(e)}'
        status_code = 503
    try:
        disk = shutil.disk_usage('/')
        health['disk_space'] = {'total_gb': disk.total // (2**30), 'free_gb': disk.free // (2**30)}
        if (disk.free / disk.total) < 0.1: # 少於 10%
             health['status'] = 'degraded'
             health['disk_space']['warning'] = 'Low disk space'
             status_code = 503
    except Exception as e:
        health['status'] = 'degraded'
        health['disk_space'] = f'Error: {str(e)}'
        status_code = 503
    return jsonify(health), status_code

# *** 關鍵修改：修正下載的記憶體問題 ***
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

# ... (其餘路由與之前版本相同，但都建議加上 handle_route_exception)

