import os
import zipfile
import tarfile
import py7zr
import time
import math
import subprocess
import random
import string
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import threading
import queue
import re
import shutil

# --- 通用輔助函式 ---

def generate_password(length=12):
    """產生一個指定長度的隨機密碼"""import os
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

        header = file.read(8)
        file.seek(0)
        
        zip_magic_numbers = [b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08']
        if filename_lower.endswith('.zip') and not any(header.startswith(sig) for sig in zip_magic_numbers):
            raise ValueError("檔案宣稱是 ZIP 檔，但內容格式不符，可能為惡意檔案。")
        if filename_lower.endswith('.7z') and not header.startswith(b"7z\xbc\xaf'\x1c"):
            raise ValueError("檔案宣稱是 7z 檔，但內容格式不符，可能為惡意檔案。")

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
    try:
        iterations = params['iterations']
        password_file_content = "--- 壓縮密碼表 ---\n"
        formats = {'zip':'.zip', '7z':'.7z', 'targz':'.tar.gz'}
        current_file = original_file
        for i in range(1, iterations + 1):
            if tasks_collection.find_one({'_id': task_id}).get('cancel_requested'):
                update_task_log(task_id, "⚠️ 日誌: 操作已被使用者取消。"); return
            format_name = params['formats'][(i - 1) % len(params['formats'])]
            output_filename = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_layer_{i}{formats[format_name]}")
            password = None; log_pwd = "(無密碼)"
            if params['use_master_pass'] and i % params['master_pass_interval'] == 0:
                password = params['master_pass']; log_pwd = "(特殊密碼層)"
            elif (params['encrypt_odd'] and i % 2 != 0) or (not params['encrypt_odd'] and i in params['manual_layers']):
                if format_name in ('zip', '7z'):
                    password = generate_password(); log_pwd = password
            password_file_content += f"第 {i} 層 ({os.path.basename(output_filename)}): {log_pwd}\n"
            progress_text = f"正在壓縮第 {i}/{iterations} 層 (格式: {format_name})"
            update_task_log(task_id, f"--- {progress_text} ---", is_progress_text=True)
            if format_name in ('zip', '7z'):
                with py7zr.SevenZipFile(output_filename, 'w', password=password) as z: z.write(current_file, os.path.basename(current_file))
            else:
                with tarfile.open(output_filename, 'w:gz') as tf: tf.add(current_file, arcname=os.path.basename(current_file))
            if current_file != original_file: os.remove(current_file)
            current_file = output_filename
            update_task_progress(task_id, int((i / iterations) * 100))
        update_task_log(task_id, "✅ 壓縮流程結束。", is_progress_text=True)
        with open(current_file, 'rb') as f_in:
            file_id = fs.put(f_in, filename=os.path.basename(current_file))
        os.remove(current_file)
        delete_token = secrets.token_hex(16)
        tasks_collection.update_one({'_id': task_id}, {'$set': { 
            'status': '完成', 'progress': 100, 
            'result_file_id': str(file_id), 'result_filename': os.path.basename(current_file), 
            'password_file_content': password_file_content, 'delete_token': delete_token
        }})
        if recipient_email and host_url:
            try:
                send_completion_email(recipient_email, task_id_str, params['raw_filename'], host_url)
                update_task_log(task_id, f"✅ 已成功寄送通知信至: {recipient_email}")
            except Exception as e:
                update_task_log(task_id, f"⚠️ 寄送通知信失敗: {e}")
    except (py7zr.Bad7zFile, zipfile.BadZipFile, tarfile.ReadError) as e:
        update_task_log(task_id, f"❌ 檔案格式錯誤或已損毀: {e}")
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    except Exception as e:
        logging.error(f"壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    finally:
        if 'original_file' in locals() and os.path.exists(original_file): os.remove(original_file)

def decompression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_decompress_temp")
    try:
        password_list = params['password_list']
        master_pass = params.get('master_pass')
        if not password_list: raise ValueError("找不到可用的密碼表。")
        current_file = original_file; total_layers = len(password_list)
        total_uncompressed_size = 0
        for i, layer_info in enumerate(reversed(password_list)):
            if tasks_collection.find_one({'_id': task_id}).get('cancel_requested'):
                update_task_log(task_id, "⚠️ 日誌: 操作已被使用者取消。"); return
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
            
            next_item_path = os.path.join(output_path, extracted_items[0])
            moved_item_path = os.path.join(OUTPUT_FOLDER, extracted_items[0])
            shutil.move(next_item_path, moved_item_path)
            shutil.rmtree(output_path)
            current_file = moved_item_path
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
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    except Exception as e:
        logging.error(f"解壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    finally:
        if 'original_file' in locals() and os.path.exists(original_file): os.remove(original_file)
        if 'current_file' in locals() and os.path.exists(current_file):
            if os.path.isdir(current_file): shutil.rmtree(current_file)
            else: os.remove(current_file)
        if 'final_archive_path' in locals() and os.path.exists(final_archive_path):
             os.remove(final_archive_path)
        if os.path.exists(output_path): shutil.rmtree(output_path)

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
    global active_task_count
    if active_task_count >= MAX_CONCURRENT_TASKS:
        return jsonify({'error': '伺服器目前忙碌中，請稍後再試。'}), 429
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        file = request.files.get('file')
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

@app.route('/decompress-manual', methods=['POST'])
def decompress_manual_route():
    global active_task_count
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
        filepath = os.path.join(UPLOAD_FOLDER, f"{str(task_id)}_{secure_filename(file.filename)}")
        file.save(filepath)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'params.original_file': filepath, 'status': '處理中', 'progress_text': '準備開始...'}})
        executor.submit(task_wrapper, decompression_worker, str(task_id))
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        if 'task_id' in locals(): tasks_collection.delete_one({'_id': task_id})
        return handle_route_exception(e, 'decompress_manual')

@app.route('/start-shared-decompression/<compress_task_id>', methods=['POST'])
def start_shared_decompression(compress_task_id):
    global active_task_count
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
        
        provided_secret = request.args.get('secret')
        if not provided_secret:
            return jsonify({'error': '缺少管理員密碼'}), 401
        
        if not secrets.compare_digest(provided_secret, ADMIN_SECRET):
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
        pipeline = [{'$group': {'_id': None, 'total_size': {'$sum': '$length'}}}]
        result = list(db['fs.files'].aggregate(pipeline))
        used_space = result[0]['total_size'] if result else 0
        return jsonify({'used_space': used_space, 'total_space': 512 * 1024 * 1024})
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


    characters = string.ascii_letters + string.digits + string.punctuation
    return ''.join(random.choice(characters) for i in range(length))

def human_readable_size(size_bytes):
    """將 bytes 轉換成易讀格式 (KB, MB, GB)"""
    if size_bytes <= 0: return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024))) if size_bytes > 0 else 0
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def get_file_type(filename):
    """根據副檔名判斷檔案類型"""
    if filename.endswith('.zip'): return 'zip'
    if filename.endswith('.7z'): return '7z'
    if filename.endswith('.rar'): return 'rar'
    if filename.endswith(('.tar.gz', '.tgz')): return 'targz'
    if filename.endswith(('.tar.bz2', '.tbz2')): return 'tarbz2'
    if filename.endswith(('.tar.xz', '.txz')): return 'tarxz'
    if filename.endswith('.tar'): return 'tar'
    return 'unknown'

# --- 編碼器核心邏輯 ---

def perform_secure_compression(params, log_queue, cancel_event):
    """執行加密壓縮的核心函式"""
    temp_files = []
    current_file_to_compress = None # <-- 關鍵修正：初始化變數
    try:
        original_file = params['original_file']
        iterations = params['iterations']
        winrar_executable_path = params['winrar_path']
        encrypt_all_odd_layers = params['encrypt_odd']
        layers_to_encrypt = params['manual_layers']
        compression_formats_sequence = params['formats']
        output_dir = params['output_dir']
        delete_original = params['delete_original']
        
        output_prefix = 'secure_layer_'
        source_basename = os.path.splitext(os.path.basename(original_file))[0]
        password_file = os.path.join(output_dir, f'{source_basename}_passwords.txt')

        if not os.path.exists(original_file):
            raise FileNotFoundError(f"找不到原始檔案 '{original_file}'")
        
        os.makedirs(output_dir, exist_ok=True)
        initial_size = os.path.getsize(original_file)
        log_queue.put(f"原始檔案 '{os.path.basename(original_file)}' 的大小: {human_readable_size(initial_size)}\n")

        with open(password_file, 'w', encoding='utf-8') as pf:
            pf.write("--- 壓縮密碼表 ---\n")
        log_queue.put(f"已建立新的密碼表於: '{password_file}'")

        formats = {'zip':'.zip', '7z':'.7z', 'rar':'.rar', 'targz':'.tar.gz', 'tarbz2':'.tar.bz2', 'tarxz':'.tar.xz'}
        
        current_file_to_compress = os.path.join(output_dir, os.path.basename(original_file))
        shutil.copy(original_file, current_file_to_compress)
        temp_files.append(current_file_to_compress)

        start_time = time.time()
        log_queue.put(('progress_max', iterations))

        for i in range(1, iterations + 1):
            if cancel_event.is_set():
                log_queue.put("\n⚠️ 操作已被使用者取消。")
                return

            current_format_name = compression_formats_sequence[(i - 1) % len(compression_formats_sequence)]
            output_filename = os.path.join(output_dir, f"{output_prefix}{i}{formats[current_format_name]}")
            
            password = None
            password_log_msg = "(無密碼)"
            
            should_encrypt = (encrypt_all_odd_layers and i % 2 != 0) or (not encrypt_all_odd_layers and i in layers_to_encrypt)
            
            if should_encrypt and not current_format_name.startswith('tar'):
                password = generate_password()
                password_log_msg = password
            elif should_encrypt:
                log_queue.put(f"注意：第 {i} 層是 tar 格式，不支援加密。")
                password_log_msg = f"(不支援加密: {current_format_name})"

            with open(password_file, 'a', encoding='utf-8') as pf:
                pf.write(f"第 {i} 層 ({os.path.basename(output_filename)}): {password_log_msg}\n")
            
            log_queue.put(('progress_text', f"第 {i} / {iterations} 層..."))
            log_queue.put(f"--- 正在壓縮第 {i}/{iterations} 層 (格式: {current_format_name}, 加密: {'是' if password else '否'}) ---")

            if current_format_name in ('zip', '7z'):
                with py7zr.SevenZipFile(output_filename, 'w', password=password) as szf:
                    szf.write(current_file_to_compress, arcname=os.path.basename(current_file_to_compress))
            elif current_format_name == 'rar':
                if not os.path.exists(winrar_executable_path):
                    raise FileNotFoundError("找不到 WinRAR.exe！請安裝 WinRAR 並確認路徑設定。")
                password_switch = f'-p"{password}"' if password else '-p-'
                command = f'"{winrar_executable_path}" a -y -ibck -ep {password_switch} "{output_filename}" "{current_file_to_compress}"'
                result = subprocess.run(command, capture_output=True, text=True, encoding='cp950', errors='ignore', shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode != 0: 
                    raise Exception(f"WinRAR 錯誤: {result.stdout or result.stderr}")
            else: # tar 系列
                mode = 'w:gz' if current_format_name == 'targz' else 'w:bz2' if current_format_name == 'tarbz2' else 'w:xz'
                with tarfile.open(output_filename, mode) as tf:
                    tf.add(current_file_to_compress, arcname=os.path.basename(current_file_to_compress))
            
            log_queue.put(f"成功: '{os.path.basename(current_file_to_compress)}' -> '{os.path.basename(output_filename)}'")
            os.remove(current_file_to_compress) 
            temp_files.remove(current_file_to_compress)
            current_file_to_compress = output_filename
            temp_files.append(current_file_to_compress)

            log_queue.put(('progress', i))

        if delete_original:
            os.remove(original_file)
            log_queue.put(f"\n✅ 已根據設定刪除來源檔案: '{original_file}'")

        log_queue.put(f"\n✅ 壓縮流程結束。總耗時: {time.time() - start_time:.2f} 秒")
        log_queue.put(f"最終檔案儲存於: '{current_file_to_compress}'")
    except Exception as e:
        log_queue.put(f"\n❌ 錯誤: {e}")
    finally:
        for f in temp_files:
            if os.path.exists(f) and f != current_file_to_compress:
                try:
                    os.remove(f)
                except OSError:
                    pass
        log_queue.put("DONE")

# --- 解碼器核心邏輯 ---

def perform_secure_decompression(params, log_queue, cancel_event):
    """執行解密與解壓縮的核心函式"""
    temp_dirs = []
    current_file = None # <-- 關鍵修正：初始化變數
    try:
        file_to_decompress = params['file_to_decompress']
        password_file = params['password_file']
        winrar_executable_path = params['winrar_path']

        if not os.path.exists(file_to_decompress):
            raise FileNotFoundError(f"找不到要解壓縮的檔案 '{file_to_decompress}'")
        if not os.path.exists(password_file):
            raise FileNotFoundError(f"找不到密碼表檔案 '{password_file}'")

        log_queue.put("正在讀取密碼表...")
        passwords = {}
        with open(password_file, 'r', encoding='utf-8') as pf:
            for line in pf:
                match = re.search(r"第 (\d+) 層 \((.*?)\): (.*)", line)
                if match:
                    layer_num, filename, password = match.groups()
                    passwords[filename] = None if password == "(無密碼)" or password.startswith("(不支援") else password
        log_queue.put("密碼表讀取完畢。\n")

        target_dir = os.path.dirname(file_to_decompress)
        current_file = file_to_decompress
        
        match = re.search(r'(\d+)', os.path.basename(current_file))
        total_layers = int(match.group(1)) if match else 1
        log_queue.put(('progress_max', total_layers))

        start_time = time.time()

        for i in range(total_layers):
            if cancel_event.is_set():
                log_queue.put("\n⚠️ 操作已被使用者取消。")
                return

            file_type = get_file_type(current_file)
            if file_type == 'unknown':
                log_queue.put(f"\n✅ 解壓縮完成！最終檔案是: '{current_file}'")
                break
            
            layer_num = total_layers - i
            log_queue.put(('progress_text', f"第 {layer_num} / {total_layers} 層..."))
            log_queue.put(f"--- 正在解第 {layer_num} 層: '{os.path.basename(current_file)}' (格式: {file_type}) ---")
            
            password = passwords.get(os.path.basename(current_file))
            log_queue.put(f"使用密碼: {'是' if password else '否'}")

            output_dir = os.path.join(target_dir, f"decompress_temp_{layer_num}")
            os.makedirs(output_dir, exist_ok=True)
            temp_dirs.append(output_dir)
            
            if file_type in ('zip', '7z'):
                with py7zr.SevenZipFile(current_file, 'r', password=password) as szf:
                    szf.extractall(path=output_dir)
            elif file_type == 'rar':
                if not os.path.exists(winrar_executable_path):
                    raise FileNotFoundError("找不到 WinRAR.exe！請安裝並設定正確路徑。")
                password_switch = f'-p"{password}"' if password else '-p-'
                command = f'"{winrar_executable_path}" x -y -ibck {password_switch} "{current_file}" "{output_dir}{os.sep}"'
                result = subprocess.run(command, capture_output=True, text=True, encoding='cp950', errors='ignore', shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode != 0:
                    raise Exception(f"WinRAR 錯誤: {result.stdout or result.stderr}")
            else: # tar
                with tarfile.open(current_file, 'r:*') as tf:
                    tf.extractall(path=output_dir)
            
            extracted_files = os.listdir(output_dir)
            if not extracted_files: raise Exception("解壓縮後資料夾是空的。")
            
            extracted_filename = extracted_files[0]
            log_queue.put(f"成功解出: '{extracted_filename}'")
            
            new_file_path = os.path.join(target_dir, extracted_filename)
            os.rename(os.path.join(output_dir, extracted_filename), new_file_path)
            
            os.remove(current_file)
            shutil.rmtree(output_dir)
            temp_dirs.remove(output_dir)
            
            current_file = new_file_path
            log_queue.put(('progress', i + 1))
        
        log_queue.put(f"\n總耗時: {time.time() - start_time:.2f} 秒")
    except Exception as e:
        log_queue.put(f"\n❌ 錯誤: {e}")
    finally:
        for d in temp_dirs:
            if os.path.exists(d): shutil.rmtree(d)
        log_queue.put("DONE")

# --- GUI 類別 ---

class EncoderApp:
    def __init__(self, master):
        self.master = master
        self.master.title("編碼器")
        self.master.geometry("650x700") 
        self.log_queue = queue.Queue()
        self.cancel_event = threading.Event()

        main_frame = ttk.Frame(master, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        file_frame = ttk.LabelFrame(main_frame, text="1. 選擇檔案與路徑", padding="10")
        file_frame.pack(fill=tk.X, pady=5)
        self.file_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.winrar_path = tk.StringVar(value=r'C:\Program Files\WinRAR\WinRAR.exe')
        
        ttk.Label(file_frame, text="來源檔案:").grid(row=0, column=0, sticky="w", padx=5)
        ttk.Entry(file_frame, textvariable=self.file_path, width=60).grid(row=0, column=1, sticky="ew")
        ttk.Button(file_frame, text="瀏覽...", command=self.browse_source_file).grid(row=0, column=2, padx=5)
        
        ttk.Label(file_frame, text="輸出資料夾:").grid(row=1, column=0, sticky="w", padx=5)
        ttk.Entry(file_frame, textvariable=self.output_dir, width=60).grid(row=1, column=1, sticky="ew")
        ttk.Button(file_frame, text="瀏覽...", command=self.browse_output_dir).grid(row=1, column=2, padx=5)

        ttk.Label(file_frame, text="WinRAR 路徑:").grid(row=2, column=0, sticky="w", padx=5)
        ttk.Entry(file_frame, textvariable=self.winrar_path, width=60).grid(row=2, column=1, sticky="ew")
        ttk.Button(file_frame, text="瀏覽...", command=lambda: self.browse_file(self.winrar_path, "選擇 WinRAR.exe", [("Executable", "*.exe")])).grid(row=2, column=2, padx=5)
        file_frame.columnconfigure(1, weight=1)

        settings_frame = ttk.LabelFrame(main_frame, text="2. 壓縮設定", padding="10")
        settings_frame.pack(fill=tk.X, pady=5)
        ttk.Label(settings_frame, text="壓縮次數:").grid(row=0, column=0, sticky="w", padx=5)
        self.iterations = tk.IntVar(value=5)
        ttk.Spinbox(settings_frame, from_=1, to=10000, textvariable=self.iterations, width=8).grid(row=0, column=1, sticky="w")
        
        self.delete_original_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings_frame, text="完成後刪除來源檔案", variable=self.delete_original_var).grid(row=0, column=2, sticky='w', padx=20)

        ttk.Label(settings_frame, text="格式順序:").grid(row=1, column=0, sticky="w", padx=5)
        self.formats = tk.StringVar(value="zip, 7z, targz, tarbz2")
        ttk.Entry(settings_frame, textvariable=self.formats, width=40).grid(row=1, column=1, columnspan=3, sticky="ew")
        
        encrypt_frame = ttk.LabelFrame(main_frame, text="3. 加密規則", padding="10")
        encrypt_frame.pack(fill=tk.X, pady=5)
        self.encrypt_mode = tk.StringVar(value="odd")
        ttk.Radiobutton(encrypt_frame, text="加密所有奇數層", variable=self.encrypt_mode, value="odd", command=self.toggle_manual_entry).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(encrypt_frame, text="手動指定層數:", variable=self.encrypt_mode, value="manual", command=self.toggle_manual_entry).grid(row=1, column=0, sticky="w")
        self.manual_layers_var = tk.StringVar(value="2, 4")
        self.manual_entry = ttk.Entry(encrypt_frame, textvariable=self.manual_layers_var, width=20)
        self.manual_entry.grid(row=1, column=1, sticky="w")
        self.toggle_manual_entry()

        log_frame = ttk.LabelFrame(main_frame, text="4. 執行與日誌", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        action_frame = ttk.Frame(log_frame)
        action_frame.pack(pady=5, fill=tk.X)
        self.start_button = ttk.Button(action_frame, text="開始壓縮", command=self.start_compression)
        self.start_button.pack(side=tk.LEFT, padx=5)
        self.cancel_button = ttk.Button(action_frame, text="取消", command=self.cancel_operation, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=5)
        
        self.progress_label = ttk.Label(log_frame, text="")
        self.progress_label.pack(fill=tk.X)
        self.progress = ttk.Progressbar(log_frame, orient=tk.HORIZONTAL, length=100, mode='determinate')
        self.progress.pack(fill=tk.X, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
    
    def browse_file(self, var, title, filetypes=None):
        filename = filedialog.askopenfilename(title=title, filetypes=filetypes or [])
        if filename: var.set(filename)
    
    def browse_source_file(self):
        filename = filedialog.askopenfilename(title="選擇一個來源檔案")
        if filename:
            self.file_path.set(filename)
            self.output_dir.set(os.path.dirname(filename))

    def browse_output_dir(self):
        directory = filedialog.askdirectory(title="選擇一個輸出資料夾")
        if directory: self.output_dir.set(directory)

    def toggle_manual_entry(self):
        self.manual_entry.config(state=tk.NORMAL if self.encrypt_mode.get() == "manual" else tk.DISABLED)

    # *** 關鍵修正：為儀表板裝上「軍規級保險絲」 ***
    def process_queue(self):
        try:
            # 一次處理佇列中的所有訊息，避免介面延遲
            while not self.log_queue.empty():
                msg = self.log_queue.get_nowait()
                if isinstance(msg, tuple):
                    if msg[0] == 'progress': self.progress['value'] = msg[1]
                    elif msg[0] == 'progress_max': self.progress['maximum'] = msg[1]
                    elif msg[0] == 'progress_text': self.progress_label.config(text=msg[1])
                elif msg == "DONE":
                    self.start_button.config(state=tk.NORMAL)
                    self.cancel_button.config(state=tk.DISABLED)
                    self.progress_label.config(text="任務完成！")
                    return # 任務結束，停止輪詢
                else:
                    self.log_text.config(state=tk.NORMAL)
                    self.log_text.insert(tk.END, str(msg) + "\n")
                    self.log_text.see(tk.END)
                    self.log_text.config(state=tk.DISABLED)
        except Exception as e:
             # 如果處理訊息時發生任何錯誤，將其顯示在日誌中，而不是讓程式崩潰
             error_message = f"\n❌ 發生未預期的 GUI 錯誤: {e}\n"
             self.log_text.config(state=tk.NORMAL)
             self.log_text.insert(tk.END, error_message)
             self.log_text.see(tk.END)
             self.log_text.config(state=tk.DISABLED)
             self.start_button.config(state=tk.NORMAL) # 重設按鈕狀態
             self.cancel_button.config(state=tk.DISABLED)
             return # 停止輪詢

        # 如果任務仍在進行中，則安排下一次檢查
        if self.start_button['state'] == tk.DISABLED:
            self.master.after(100, self.process_queue)

    def start_compression(self):
        self.log_text.config(state=tk.NORMAL); self.log_text.delete(1.0, tk.END); self.log_text.config(state=tk.DISABLED)
        self.cancel_event.clear()
        self.progress_label.config(text="")
        params = {}
        try:
            params['original_file'] = self.file_path.get()
            params['output_dir'] = self.output_dir.get()
            if not params['original_file']: raise ValueError("請先選擇一個來源檔案！")
            if not params['output_dir']: raise ValueError("請先選擇一個輸出資料夾！")
            params['iterations'] = self.iterations.get()
            params['delete_original'] = self.delete_original_var.get()
            self.progress['maximum'] = params['iterations']
            self.progress['value'] = 0
            params['winrar_path'] = self.winrar_path.get()
            params['encrypt_odd'] = self.encrypt_mode.get() == "odd"
            params['manual_layers'] = {int(x.strip()) for x in self.manual_layers_var.get().split(',') if x.strip()} if not params['encrypt_odd'] else set()
            params['formats'] = [x.strip() for x in self.formats.get().split(',') if x.strip()]
            if not params['formats']: raise ValueError("請至少提供一種壓縮格式。")
        except Exception as e: # 使用更通用的 Exception 來捕捉所有可能的錯誤
            self.log_text.config(state=tk.NORMAL); self.log_text.insert(tk.END, f"錯誤：{e}\n"); self.log_text.config(state=tk.DISABLED)
            return
        
        self.start_button.config(state=tk.DISABLED)
        self.cancel_button.config(state=tk.NORMAL)
        threading.Thread(target=perform_secure_compression, args=(params, self.log_queue, self.cancel_event), daemon=True).start()
        self.process_queue()
    
    def cancel_operation(self):
        self.cancel_event.set()

class DecoderApp(EncoderApp): # Decoder 也繼承這些修正
    def __init__(self, master):
        self.master = master
        self.master.title("解碼器")
        self.master.geometry("650x550")
        self.log_queue = queue.Queue()
        self.cancel_event = threading.Event()

        main_frame = ttk.Frame(master, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        file_frame = ttk.LabelFrame(main_frame, text="1. 選擇檔案", padding="10")
        file_frame.pack(fill=tk.X, pady=5)
        self.file_path = tk.StringVar()
        self.password_path = tk.StringVar()
        self.winrar_path = tk.StringVar(value=r'C:\Program Files\WinRAR\WinRAR.exe')
        
        ttk.Label(file_frame, text="壓縮檔案:").grid(row=0, column=0, sticky="w", padx=5)
        ttk.Entry(file_frame, textvariable=self.file_path, width=60).grid(row=0, column=1, sticky="ew")
        ttk.Button(file_frame, text="瀏覽...", command=self.browse_compressed_file).grid(row=0, column=2, padx=5)

        ttk.Label(file_frame, text="密碼表:").grid(row=1, column=0, sticky="w", padx=5)
        ttk.Entry(file_frame, textvariable=self.password_path, width=60).grid(row=1, column=1, sticky="ew")
        ttk.Button(file_frame, text="瀏覽...", command=lambda: self.browse_file(self.password_path, "選擇 passwords.txt", [("Text files", "*.txt")])).grid(row=1, column=2, padx=5)

        ttk.Label(file_frame, text="WinRAR 路徑:").grid(row=2, column=0, sticky="w", padx=5)
        ttk.Entry(file_frame, textvariable=self.winrar_path, width=60).grid(row=2, column=1, sticky="ew")
        ttk.Button(file_frame, text="瀏覽...", command=lambda: self.browse_file(self.winrar_path, "選擇 WinRAR.exe", [("Executable", "*.exe")])).grid(row=2, column=2, padx=5)
        file_frame.columnconfigure(1, weight=1)

        log_frame = ttk.LabelFrame(main_frame, text="2. 執行與日誌", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        action_frame = ttk.Frame(log_frame)
        action_frame.pack(pady=5, fill=tk.X)
        self.start_button = ttk.Button(action_frame, text="開始解壓縮", command=self.start_decompression)
        self.start_button.pack(side=tk.LEFT, padx=5)
        self.cancel_button = ttk.Button(action_frame, text="取消", command=self.cancel_operation, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=5)
        
        self.progress_label = ttk.Label(log_frame, text="")
        self.progress_label.pack(fill=tk.X)
        self.progress = ttk.Progressbar(log_frame, orient=tk.HORIZONTAL, length=100, mode='determinate')
        self.progress.pack(fill=tk.X, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def browse_compressed_file(self):
        filename = filedialog.askopenfilename(title="選擇要解壓的檔案")
        if filename:
            self.file_path.set(filename)
            base_dir = os.path.dirname(filename)
            base_name_match = re.match(r'(.+?)_secure_layer_', os.path.basename(filename))
            if base_name_match:
                possible_pass_file = os.path.join(base_dir, f"{base_name_match.group(1)}_passwords.txt")
                if os.path.exists(possible_pass_file):
                    self.password_path.set(possible_pass_file)
                    return
            
            generic_pass_file = os.path.join(base_dir, 'passwords.txt')
            if os.path.exists(generic_pass_file):
                self.password_path.set(generic_pass_file)

    def start_decompression(self):
        self.log_text.config(state=tk.NORMAL); self.log_text.delete(1.0, tk.END); self.log_text.config(state=tk.DISABLED)
        self.cancel_event.clear()
        self.progress['value'] = 0
        self.progress_label.config(text="")
        params = {}
        try:
            params['file_to_decompress'] = self.file_path.get()
            params['password_file'] = self.password_path.get()
            if not params['file_to_decompress']: raise ValueError("請選擇要解壓縮的檔案！")
            if not params['password_file']: raise ValueError("請選擇密碼表檔案！")
            params['winrar_path'] = self.winrar_path.get()
        except Exception as e:
            self.log_text.config(state=tk.NORMAL); self.log_text.insert(tk.END, f"錯誤：{e}\n"); self.log_text.config(state=tk.DISABLED)
            return

        self.start_button.config(state=tk.DISABLED)
        self.cancel_button.config(state=tk.NORMAL)
        threading.Thread(target=perform_secure_decompression, args=(params, self.log_queue, self.cancel_event), daemon=True).start()
        self.process_queue()

# --- 主啟動器 ---
class MainApp:
    def __init__(self, root):
        self.root = root
        self.root.title("多功能壓縮工具")
        self.root.geometry("300x150")
        style = ttk.Style()
        style.configure("Big.TButton", font=("", 12), padding=10)
        main_frame = ttk.Frame(root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)
        encoder_button = ttk.Button(main_frame, text="開啟編碼器", style="Big.TButton", command=self.open_encoder)
        encoder_button.pack(fill=tk.X, pady=5)
        decoder_button = ttk.Button(main_frame, text="開啟解碼器", style="Big.TButton", command=self.open_decoder)
        decoder_button.pack(fill=tk.X, pady=5)

    def open_window(self, app_class):
        window = tk.Toplevel(self.root)
        app = app_class(window)

    def open_encoder(self):
        self.open_window(EncoderApp)

    def open_decoder(self):
        self.open_window(DecoderApp)

if __name__ == "__main__":
    root = tk.Tk()
    app = MainApp(root)
    root.mainloop()

