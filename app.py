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
from flask import Flask, request, jsonify, send_from_directory, render_template
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import logging
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 設定 ---
UPLOAD_FOLDER = '/tmp/compressor_uploads'
OUTPUT_FOLDER = '/tmp/compressor_outputs'
MAX_FILE_AGE = timedelta(hours=1)

for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- 資料庫與加密金鑰連線 ---
MONGO_URI = os.environ.get('MONGO_URI')
SECRET_KEY = os.environ.get('SECRET_KEY') # 萬能總鑰

client = None
db = None
tasks_collection = None
password_maps_collection = None
db_connection_error = None
cipher_suite = None

try:
    if not MONGO_URI:
        raise ValueError("錯誤：找不到 MONGO_URI 環境變數。")
    if not SECRET_KEY:
        raise ValueError("安全性錯誤：找不到 SECRET_KEY 環境變數。請在 Zeabur 中設定一個長的隨機字串以啟用加密功能。")
    
    # 使用 SHA256 來確保金鑰長度為 32 bytes
    key = hashlib.sha256(SECRET_KEY.encode()).digest()
    cipher_suite = Fernet(key) # 初始化加密套件

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logging.info("✅ 成功連線至 MongoDB！")
    db = client['compressor_db']
    tasks_collection = db['tasks']
    password_maps_collection = db['password_maps']
    password_maps_collection.create_index([("sha256", 1), ("pin_hash", 1)])
except Exception as e:
    db_connection_error = e
    logging.error(f"❌ 應用程式啟動失敗: {e}")

# --- 自動清理函式 ---
def cleanup_old_files():
    global cleanup_timer
    try:
        now = datetime.now()
        for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path):
                    mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if now - mod_time > MAX_FILE_AGE:
                        os.remove(file_path)
    except Exception as e:
        logging.error(f"檔案清理任務發生錯誤: {e}")
    finally:
        cleanup_timer = threading.Timer(1800, cleanup_old_files)
        cleanup_timer.start()

# --- 通用輔助函式 ---
def generate_password(length=12):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

def update_task_log(task_id, message):
    tasks_collection.update_one({'_id': task_id}, {'$push': {'logs': message}})

def update_task_progress(task_id, progress):
    tasks_collection.update_one({'_id': task_id}, {'$set': {'progress': progress}})

def calculate_sha256(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def hash_pin(pin):
    """新增：將 PIN 碼進行雜湊處理"""
    return hashlib.sha256(pin.encode('utf-8')).hexdigest()

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

# --- 壓縮背景任務 ---
def compression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id})
    if not task: return

    params = task['params']
    original_file = params['original_file']
    
    try:
        original_sha256 = calculate_sha256(original_file)
        update_task_log(task_id, f"日誌: SHA-256: {original_sha256[:12]}...")

        iterations, encrypt_odd, manual_layers, formats_seq, \
        use_master_pass, master_pass, master_pass_interval, pin_code = \
            params['iterations'], params['encrypt_odd'], params['manual_layers'], params['formats'], \
            params['use_master_pass'], params['master_pass'], params['master_pass_interval'], params['pin_code']
        
        password_file_content = "--- 壓縮密碼表 ---\n"
        formats = {'zip':'.zip', '7z':'.7z', 'targz':'.tar.gz', 'tarbz2':'.tar.bz2', 'tarxz':'.tar.xz'}
        current_file = original_file

        for i in range(1, iterations + 1):
            task_check = tasks_collection.find_one({'_id': task_id}, {'cancel_requested': 1})
            if task_check and task_check.get('cancel_requested'):
                update_task_log(task_id, "⚠️ 日誌: 操作已被使用者取消。")
                tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '已取消'}})
                return

            format_name = formats_seq[(i - 1) % len(formats_seq)]
            output_filename = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_layer_{i}{formats[format_name]}")
            
            password = None
            log_pwd = "(無密碼)"
            
            if use_master_pass and i % master_pass_interval == 0:
                password = master_pass
                log_pwd = "(特殊密碼層)"
            else:
                should_encrypt = (encrypt_odd and i % 2 != 0) or (not encrypt_odd and i in manual_layers)
                if should_encrypt and format_name in ('zip', '7z'):
                    password = generate_password()
                    log_pwd = password

            password_file_content += f"第 {i} 層 ({os.path.basename(output_filename)}): {log_pwd}\n"
            update_task_log(task_id, f"--- 第 {i}/{iterations} 層 (格式: {format_name}, 加密: {'是' if log_pwd != '(無密碼)' else '否'}) ---")

            if format_name in ('zip', '7z'):
                with py7zr.SevenZipFile(output_filename, 'w', password=password) as z: z.write(current_file, os.path.basename(current_file))
            else:
                mode = {'targz': 'w:gz', 'tarbz2': 'w:bz2', 'tarxz': 'w:xz'}[format_name]
                with tarfile.open(output_filename, mode) as tf: tf.add(current_file, arcname=os.path.basename(current_file))
            
            if current_file != original_file: os.remove(current_file)
            current_file = output_filename
            update_task_progress(task_id, int((i / iterations) * 100))

        tasks_collection.update_one({'_id': task_id}, {'$set': { 'status': '完成', 'progress': 100, 'result_file': os.path.basename(current_file), 'password_file_content': password_file_content, 'original_sha256': original_sha256 }})
        update_task_log(task_id, "✅ 壓縮流程結束。")

        parsed_passwords = parse_password_text(password_file_content)
        if parsed_passwords:
            map_to_save = { 'passwords': parsed_passwords, 'original_filename': params['raw_filename'], 'created_at': datetime.utcnow() }
            if use_master_pass and pin_code:
                map_to_save['pin_hash'] = hash_pin(pin_code)
                map_to_save['encrypted_master_pass'] = cipher_suite.encrypt(master_pass.encode('utf-8'))
            
            password_maps_collection.update_one( {'sha256': original_sha256}, {'$set': map_to_save}, upsert=True)
            update_task_log(task_id, f"日誌: 密碼表已安全儲存。")
    except Exception as e:
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗'}})
        update_task_log(task_id, f"❌ 嚴重錯誤: {e}")
    finally:
        if os.path.exists(original_file): os.remove(original_file)
        if 'current_file' in locals() and os.path.exists(current_file) and tasks_collection.find_one({'_id': task_id}).get('status') != '完成':
             os.remove(current_file)


# --- 解壓縮背景任務 ---
def decompression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id})
    if not task: return

    params = task['params']
    original_file, password_list, master_pass = \
        params['original_file'], params['password_list'], params.get('master_pass')

    try:
        update_task_log(task_id, f"開始解壓縮檔案: {os.path.basename(original_file)}")
        if not password_list:
            raise ValueError("找不到可用的密碼表。")

        current_file = original_file
        total_layers = len(password_list)

        for i, layer_info in enumerate(reversed(password_list)):
            task_check = tasks_collection.find_one({'_id': task_id}, {'cancel_requested': 1})
            if task_check and task_check.get('cancel_requested'):
                update_task_log(task_id, "⚠️ 日誌: 操作已被使用者取消。")
                tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '已取消'}})
                return

            layer_num = total_layers - i
            filename = layer_info['filename']
            password = layer_info['password']
            
            # *** 關鍵修改：新增更清晰、更安全的日誌訊息 ***
            password_usage_log = "否" # 預設為無密碼
            
            if password == 'MASTER_PASSWORD_PLACEHOLDER':
                if not master_pass:
                    raise ValueError(f"第 {layer_num} 層需要特殊密碼，但您沒有提供或系統無法自動帶入。")
                password = master_pass
                password_usage_log = "是 (使用您提供的特殊密碼)"
            elif password:
                password_usage_log = "是 (使用儲存的隨機密碼)"

            update_task_log(task_id, f"--- 第 {layer_num}/{total_layers} 層 ({filename}) ---")
            update_task_log(task_id, f"日誌: 本層是否需要密碼: {password_usage_log}")
            
            output_path = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_decompress_temp")
            
            if filename.endswith(('.zip', '.7z')):
                with py7zr.SevenZipFile(current_file, 'r', password=password) as z:
                    z.extractall(path=output_path)
            elif any(filename.endswith(ext) for ext in ['.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar']):
                with tarfile.open(current_file, 'r:*') as tf:
                    tf.extractall(path=output_path)
            else:
                raise ValueError(f"不支援的檔案格式: {filename}")
            
            if current_file != original_file: os.remove(current_file)
            
            extracted_files = os.listdir(output_path)
            if not extracted_files: raise ValueError("解壓縮後找不到任何檔案。")

            extracted_file_path = os.path.join(output_path, extracted_files[0])
            shutil.move(extracted_file_path, os.path.join(OUTPUT_FOLDER, extracted_files[0]))
            shutil.rmtree(output_path)
            current_file = os.path.join(OUTPUT_FOLDER, extracted_files[0])
            
            update_task_progress(task_id, int(((i + 1) / total_layers) * 100))

        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '完成', 'progress': 100, 'result_file': os.path.basename(current_file)}})
        update_task_log(task_id, "✅ 解壓縮流程結束。")
    except Exception as e:
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗'}})
        update_task_log(task_id, f"❌ 錯誤: {e}")
    finally:
        if os.path.exists(original_file): os.remove(original_file)
        if 'current_file' in locals() and os.path.exists(current_file) and tasks_collection.find_one({'_id': task_id}).get('status') != '完成':
             os.remove(current_file)

# --- API 路由 ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/compress', methods=['POST'])
def compress_route():
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    if 'file' not in request.files: return jsonify({'error': '沒有上傳檔案'}), 400
    
    file = request.files['file']
    filename = file.filename
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        use_master_pass = request.form.get('use_master_pass') == 'on'
        master_pass = request.form.get('master_password') if use_master_pass else None
        master_pass_interval = int(request.form.get('master_password_interval', '10')) if use_master_pass else 0
        pin_code = request.form.get('pin_code') if use_master_pass else None

        if use_master_pass and not master_pass: raise ValueError("您已啟用特殊密碼，但未提供密碼。")

        params = {
            'original_file': filepath, 'raw_filename': filename,
            'iterations': int(request.form.get('iterations', 5)),
            'encrypt_odd': request.form.get('encrypt_mode', 'odd') == 'odd',
            'manual_layers': [int(x.strip()) for x in request.form.get('manual_layers', '').split(',') if x.strip()],
            'formats': [x.strip() for x in request.form.get('formats', 'zip,7z,targz').split(',') if x.strip()],
            'use_master_pass': use_master_pass, 'master_pass': master_pass,
            'master_pass_interval': master_pass_interval, 'pin_code': pin_code,
        }
        task = {'type': 'compress', 'status': '處理中', 'progress': 0, 'logs': [f"收到檔案 '{filename}'"], 'params': params, 'created_at': datetime.utcnow(), 'cancel_requested': False}
        task_id = tasks_collection.insert_one(task).inserted_id
        threading.Thread(target=compression_worker, args=(str(task_id),)).start()
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/decompress', methods=['POST'])
def decompress_route():
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    if 'file' not in request.files: return jsonify({'error': '沒有上傳檔案'}), 400

    file = request.files['file']
    filename = file.filename
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        file_sha256 = calculate_sha256(filepath)
        pin_code = request.form.get('pin_code')
        manual_master_pass = request.form.get('master_password')
        password_text = request.form.get('passwords', '')
        
        password_list = []
        master_pass_for_worker = None
        logs = [f"收到檔案 '{filename}'", f"日誌: 檔案 SHA-256: {file_sha256[:12]}..."]

        if pin_code:
            logs.append(f"日誌: 偵測到 PIN 碼，嘗試驗證...")
            hashed_pin_attempt = hash_pin(pin_code)
            password_map = password_maps_collection.find_one({'sha256': file_sha256, 'pin_hash': hashed_pin_attempt})
            if password_map:
                password_list = password_map.get('passwords', [])
                encrypted_pass = password_map.get('encrypted_master_pass')
                master_pass_for_worker = cipher_suite.decrypt(encrypted_pass).decode('utf-8')
                logs.append("✅ 日誌: PIN 碼驗證成功！已自動解密並載入特殊密碼。")
            else:
                raise ValueError("PIN 碼錯誤或與此檔案不匹配。")
        else:
            logs.append("日誌: 未輸入 PIN 碼，嘗試尋找公開密碼表...")
            password_map = password_maps_collection.find_one({'sha256': file_sha256})
            if password_map:
                password_list = password_map.get('passwords', [])
                master_pass_for_worker = manual_master_pass
                logs.append("✅ 日誌: 成功從資料庫找到匹配的密碼表。")
            elif password_text:
                logs.append("⚠️ 日誌: 未在資料庫中找到匹配項，將使用您貼上的密碼表。")
                password_list = parse_password_text(password_text)
                master_pass_for_worker = manual_master_pass
        
        if not password_list:
            raise ValueError("找不到匹配或提供的密碼表。")

        params = {'original_file': filepath, 'password_list': password_list, 'master_pass': master_pass_for_worker}
        task = {'type': 'decompress', 'status': '處理中', 'progress': 0, 'logs': logs, 'params': params, 'created_at': datetime.utcnow(), 'cancel_requested': False}
        task_id = tasks_collection.insert_one(task).inserted_id
        threading.Thread(target=decompression_worker, args=(str(task_id),)).start()
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        if os.path.exists(filepath): os.remove(filepath)
        return jsonify({'error': str(e)}), 400

@app.route('/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    try:
        tasks_collection.update_one({'_id': ObjectId(task_id)}, {'$set': {'cancel_requested': True}})
        return jsonify({'status': 'cancellation requested'})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/status/<task_id>')
def task_status(task_id):
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    task = tasks_collection.find_one({'_id': ObjectId(task_id)})
    if task:
        task['_id'] = str(task['_id'])
        return jsonify(task)
    return jsonify({'error': '找不到任務'}), 404

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)

@app.route('/health')
def health_check():
    if db_connection_error: return jsonify({'status': 'error', 'db': 'failed'}), 500
    try:
        client.admin.command('ping')
        return jsonify({'status': 'ok', 'db': 'successful'})
    except Exception as e:
        return jsonify({'status': 'error', 'db': 'ping_failed', 'error': str(e)}), 500

cleanup_timer = threading.Timer(10, cleanup_old_files)
cleanup_timer.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

