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
from flask import Flask, request, jsonify, send_from_directory, render_template, redirect, send_file
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import logging
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet
from gridfs import GridFS

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 設定 ---
UPLOAD_FOLDER = '/tmp/compressor_uploads'
OUTPUT_FOLDER = '/tmp/compressor_outputs'

for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- 資料庫與加密金鑰連線 ---
MONGO_URI = os.environ.get('MONGO_URI')
SECRET_KEY = os.environ.get('SECRET_KEY')

client = None; db = None; tasks_collection = None; db_connection_error = None; cipher_suite = None; fs = None

try:
    if not MONGO_URI: raise ValueError("錯誤：找不到 MONGO_URI 環境變數。")
    if not SECRET_KEY: raise ValueError("安全性錯誤：找不到 SECRET_KEY 環境變數。")
    
    key_material = hashlib.sha256(SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(key_material)
    cipher_suite = Fernet(key)

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logging.info("✅ 成功連線至 MongoDB！")
    db = client['compressor_db']
    tasks_collection = db['tasks']
    fs = GridFS(db) # 初始化 GridFS
except Exception as e:
    db_connection_error = e
    logging.error(f"❌ 應用程式啟動失敗: {e}")

# --- 通用輔助函式 ---
def generate_password(length=12):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))
def update_task_log(task_id, message):
    tasks_collection.update_one({'_id': task_id}, {'$push': {'logs': message}})
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

# --- 背景任務 ---
def compression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']
    
    try:
        iterations, use_master_pass, master_pass, master_pass_interval = \
            params['iterations'], params['use_master_pass'], params['master_pass'], params['master_pass_interval']
        
        password_file_content = "--- 壓縮密碼表 ---\n"
        formats = {'zip':'.zip', '7z':'.7z', 'targz':'.tar.gz', 'tarbz2':'.tar.bz2', 'tarxz':'.tar.xz'}
        current_file = original_file

        for i in range(1, iterations + 1):
            if tasks_collection.find_one({'_id': task_id}).get('cancel_requested'):
                update_task_log(task_id, "⚠️ 日誌: 操作已被使用者取消。"); return

            format_name = params['formats'][(i - 1) % len(params['formats'])]
            output_filename = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_layer_{i}{formats[format_name]}")
            
            password = None; log_pwd = "(無密碼)"
            if use_master_pass and i % master_pass_interval == 0:
                password = master_pass; log_pwd = "(特殊密碼層)"
            elif (params['encrypt_odd'] and i % 2 != 0) or (not params['encrypt_odd'] and i in params['manual_layers']):
                if format_name in ('zip', '7z'):
                    password = generate_password(); log_pwd = password

            password_file_content += f"第 {i} 層 ({os.path.basename(output_filename)}): {log_pwd}\n"
            update_task_log(task_id, f"--- 第 {i}/{iterations} 層 (格式: {format_name}, 加密: {'是' if password else '否'}) ---")

            if format_name in ('zip', '7z'):
                with py7zr.SevenZipFile(output_filename, 'w', password=password) as z: z.write(current_file, os.path.basename(current_file))
            else: # TAR
                mode = {'targz': 'w:gz', 'tarbz2': 'w:bz2', 'tarxz': 'w:xz'}[format_name]
                with tarfile.open(output_filename, mode) as tf: tf.add(current_file, arcname=os.path.basename(current_file))
            
            if current_file != original_file: os.remove(current_file)
            current_file = output_filename
            update_task_progress(task_id, int((i / iterations) * 100))
        
        update_task_log(task_id, "✅ 壓縮流程結束。")
        final_filename = os.path.basename(current_file)

        update_task_log(task_id, "日誌: 正在將最終檔案存入安全資料庫...")
        with open(current_file, 'rb') as f_in:
            file_id = fs.put(f_in, filename=final_filename)
        update_task_log(task_id, "✅ 檔案儲存成功！")
        
        os.remove(current_file)

        tasks_collection.update_one({'_id': task_id}, {'$set': { 
            'status': '完成', 
            'progress': 100, 
            'result_file_id': str(file_id),
            'result_filename': final_filename, 
            'password_file_content': password_file_content 
        }})
    except Exception as e:
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗'}})
        update_task_log(task_id, f"❌ 嚴重錯誤: {e}")
    finally:
        if os.path.exists(original_file): os.remove(original_file)

def decompression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']

    try:
        password_list, master_pass = params['password_list'], params.get('master_pass')
        update_task_log(task_id, f"開始解壓縮檔案: {os.path.basename(original_file)}")
        if not password_list: raise ValueError("找不到可用的密碼表。")

        current_file = original_file; total_layers = len(password_list)

        for i, layer_info in enumerate(reversed(password_list)):
            if tasks_collection.find_one({'_id': task_id}).get('cancel_requested'):
                update_task_log(task_id, "⚠️ 日誌: 操作已被使用者取消。"); return

            layer_num = total_layers - i
            filename, password = layer_info['filename'], layer_info['password']
            password_usage_log = "否"
            if password == 'MASTER_PASSWORD_PLACEHOLDER':
                if not master_pass: raise ValueError(f"第 {layer_num} 層需要特殊密碼。")
                password = master_pass; password_usage_log = "是 (使用特殊密碼)"
            elif password:
                password_usage_log = "是 (使用儲存的密碼)"

            update_task_log(task_id, f"--- 第 {layer_num}/{total_layers} 層 ({filename}) ---")
            update_task_log(task_id, f"日誌: 本層是否需要密碼: {password_usage_log}")
            
            output_path = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_decompress_temp")
            
            if filename.endswith(('.zip', '.7z')):
                with py7zr.SevenZipFile(current_file, 'r', password=password) as z: z.extractall(path=output_path)
            else: # TAR
                with tarfile.open(current_file, 'r:*') as tf: tf.extractall(path=output_path)
            
            if current_file != original_file: os.remove(current_file)
            
            extracted_files = os.listdir(output_path)
            if not extracted_files: raise Exception("解壓縮後找不到任何檔案。")

            new_file_path = os.path.join(OUTPUT_FOLDER, extracted_files[0])
            shutil.move(os.path.join(output_path, extracted_files[0]), new_file_path)
            shutil.rmtree(output_path)
            current_file = new_file_path
            
            update_task_progress(task_id, int(((i + 1) / total_layers) * 100))

        # *** 關鍵修改：使用原始檔名來儲存最終檔案 ***
        final_filename_from_archive = os.path.basename(current_file)
        # 從參數中取得壓縮時儲存的原始檔名
        expected_filename = params.get('expected_filename')
        
        # 如果有預期的檔名，就使用它；否則，使用從壓縮檔中解出的名稱
        final_filename_to_store = expected_filename if expected_filename else final_filename_from_archive

        with open(current_file, 'rb') as f_in:
            file_id = fs.put(f_in, filename=final_filename_to_store)
        os.remove(current_file)

        tasks_collection.update_one({'_id': task_id}, {'$set': {
            'status': '完成', 
            'progress': 100, 
            'result_file_id': str(file_id),
            'result_filename': final_filename_to_store # 使用正確的檔名
        }})
        update_task_log(task_id, "✅ 解壓縮流程結束。")
    except Exception as e:
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗'}})
        update_task_log(task_id, f"❌ 錯誤: {e}")
    finally:
        if os.path.exists(original_file): os.remove(original_file)

# --- API 路由 ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/compress', methods=['POST'])
def compress_route():
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    if 'file' not in request.files: return jsonify({'error': '沒有上傳檔案'}), 400
    
    file = request.files['file']; filename = secure_filename(file.filename)
    try:
        params = {
            'raw_filename': filename, 'iterations': int(request.form.get('iterations', 5)),
            'encrypt_odd': request.form.get('encrypt_mode', 'odd') == 'odd',
            'manual_layers': [int(x.strip()) for x in request.form.get('manual_layers', '').split(',') if x.strip()],
            'formats': [x.strip() for x in request.form.get('formats', 'zip,7z,targz').split(',') if x.strip()],
            'use_master_pass': request.form.get('use_master_pass') == 'on',
            'master_pass': request.form.get('master_password'),
            'master_pass_interval': int(request.form.get('master_password_interval', '10'))
        }
        if params['use_master_pass'] and not params['master_pass']: raise ValueError("已啟用特殊密碼，但未提供。")
        
        task = {'type': 'compress', 'status': 'pending', 'params': params, 'created_at': datetime.utcnow()}
        task_id = tasks_collection.insert_one(task).inserted_id
        
        filepath = os.path.join(UPLOAD_FOLDER, f"{str(task_id)}_{filename}")
        file.save(filepath)
        
        tasks_collection.update_one({'_id': task_id}, {'$set': {'params.original_file': filepath, 'status': '處理中'}})
        threading.Thread(target=compression_worker, args=(str(task_id),)).start()
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/start-shared-decompression/<compress_task_id>', methods=['POST'])
def start_shared_decompression(compress_task_id):
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    try:
        original_task = tasks_collection.find_one({'_id': ObjectId(compress_task_id)})
        if not original_task or 'result_file_id' not in original_task: raise ValueError("找不到原始壓縮任務。")
        
        file_id = ObjectId(original_task['result_file_id'])
        filename = original_task['result_filename']
        
        grid_out = fs.get(file_id)
        filepath = os.path.join(UPLOAD_FOLDER, f"share_{filename}")
        with open(filepath, 'wb') as f_out:
            f_out.write(grid_out.read())

        params = {
            'original_file': filepath,
            'password_list': parse_password_text(original_task.get('password_file_content', '')),
            'master_pass': request.get_json().get('master_password'),
            # *** 關鍵修改：將原始檔名傳遞給解壓縮任務 ***
            'expected_filename': original_task.get('params', {}).get('raw_filename')
        }
        
        new_task = {'type': 'decompress', 'status': '處理中', 'params': params, 'created_at': datetime.utcnow()}
        new_task_id = tasks_collection.insert_one(new_task).inserted_id
        
        threading.Thread(target=decompression_worker, args=(str(new_task_id),)).start()
        return jsonify({'task_id': str(new_task_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# 新增：儲存空間統計 API
@app.route('/storage-stats')
def storage_stats():
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    try:
        stats = db.command('dbstats')
        # fsUsedSize 是 GridFS 已使用的空間 (以 bytes 為單位)
        used_space = stats.get('fsUsedSize', 0)
        # 免費方案的總空間是 512 MB
        total_space = 512 * 1024 * 1024
        return jsonify({'used_space': used_space, 'total_space': total_space})
    except Exception as e:
        logging.error(f"無法取得儲存統計資料: {e}")
        return jsonify({'error': '無法取得統計資料'}), 500

@app.route('/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    tasks_collection.update_one({'_id': ObjectId(task_id)}, {'$set': {'cancel_requested': True}})
    return jsonify({'status': 'cancellation requested'})

@app.route('/status/<task_id>')
def task_status(task_id):
    task = tasks_collection.find_one({'_id': ObjectId(task_id)})
    if task:
        task['_id'] = str(task['_id']); return jsonify(task)
    return jsonify({'error': '找不到任務'}), 404

@app.route('/download/<task_id>')
def download_file(task_id):
    try:
        task = tasks_collection.find_one({'_id': ObjectId(task_id)})
        if not task or 'result_file_id' not in task:
            return "找不到檔案紀錄。", 404
        
        file_id = ObjectId(task['result_file_id'])
        filename = task['result_filename']
        
        grid_out = fs.get(file_id)
        
        return send_file(grid_out, download_name=filename, as_attachment=True)
    except Exception as e:
        logging.error(f"下載檔案時發生錯誤: {e}")
        return "下載檔案時發生內部錯誤。", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

