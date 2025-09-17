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
from flask import Flask, request, jsonify, send_from_directory, render_template
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import logging
# werkzeug.utils 中的 secure_filename 已不再需要，但保留 import 以防萬一
from werkzeug.utils import secure_filename

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 設定 ---
UPLOAD_FOLDER = '/tmp/compressor_uploads'
OUTPUT_FOLDER = '/tmp/compressor_outputs'
MAX_FILE_AGE = timedelta(hours=1)

for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- 資料庫連線 ---
MONGO_URI = os.environ.get('MONGO_URI')
client = None
db = None
tasks_collection = None
db_connection_error = None

try:
    if not MONGO_URI:
        raise ValueError("錯誤：找不到 MONGO_URI 環境變數。")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logging.info("✅ 成功連線至 MongoDB！")
    db = client['compressor_db']
    tasks_collection = db['tasks']
except Exception as e:
    db_connection_error = e
    logging.error(f"❌ 無法連線至 MongoDB: {e}")

# --- 自動清理函式 ---
def cleanup_old_files():
    global cleanup_timer
    try:
        logging.info("執行排程任務：清理暫存檔案...")
        now = datetime.now()
        cleaned_count = 0
        for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path):
                    mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if now - mod_time > MAX_FILE_AGE:
                        os.remove(file_path)
                        logging.info(f"已刪除過期暫存檔: {file_path}")
                        cleaned_count += 1
        logging.info(f"清理完畢。共刪除 {cleaned_count} 個檔案。")
    except Exception as e:
        logging.error(f"檔案清理任務發生錯誤: {e}")
    finally:
        cleanup_timer = threading.Timer(1800, cleanup_old_files)
        cleanup_timer.start()

# --- 通用輔助函式 ---
def generate_password(length=12):
    """產生一個指定長度的隨機密碼 (只使用英文字母和數字)。"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for i in range(length))

def update_task_log(task_id, message):
    tasks_collection.update_one({'_id': task_id}, {'$push': {'logs': message}})

def update_task_progress(task_id, progress):
    tasks_collection.update_one({'_id': task_id}, {'$set': {'progress': progress}})

# --- 壓縮背景任務 ---
def compression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id})
    if not task: return

    params = task['params']
    original_file, iterations, encrypt_odd, manual_layers, formats_seq = \
        params['original_file'], params['iterations'], params['encrypt_odd'], \
        params['manual_layers'], params['formats']
    
    try:
        password_file_content = "--- 壓縮密碼表 ---\n"
        update_task_log(task_id, "日誌: 已建立密碼表。")
        formats = {'zip':'.zip', '7z':'.7z', 'targz':'.tar.gz', 'tarbz2':'.tar.bz2', 'tarxz':'.tar.xz'}
        current_file = original_file

        for i in range(1, iterations + 1):
            format_name = formats_seq[(i - 1) % len(formats_seq)]
            
            should_encrypt = (encrypt_odd and i % 2 != 0) or (not encrypt_odd and i in manual_layers)
            
            output_filename = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_layer_{i}{formats[format_name]}")
            password = None
            log_pwd = "(無密碼)"
            
            if should_encrypt and format_name in ('zip', '7z'):
                password = generate_password()
                log_pwd = password
                update_task_log(task_id, f"日誌: 為第 {i} 層產生密碼: {log_pwd}")
            elif should_encrypt and format_name.startswith('tar'):
                 update_task_log(task_id, f"注意：第 {i} 層是 {format_name} 格式，不支援加密。")
                 log_pwd = f"(不支援加密: {format_name})"

            password_file_content += f"第 {i} 層 ({os.path.basename(output_filename)}): {log_pwd}\n"
            update_task_log(task_id, f"--- 第 {i}/{iterations} 層 (格式: {format_name}, 加密: {'是' if password else '否'}) ---")

            if format_name in ('zip', '7z'):
                update_task_log(task_id, f"日誌: 使用 py7zr 壓縮 '{os.path.basename(current_file)}'...")
                with py7zr.SevenZipFile(output_filename, 'w', password=password) as z:
                    z.write(current_file, os.path.basename(current_file))
            else: # tar 系列
                mode = {'targz': 'w:gz', 'tarbz2': 'w:bz2', 'tarxz': 'w:xz'}[format_name]
                update_task_log(task_id, f"日誌: 使用 tarfile ({mode}) 壓縮 '{os.path.basename(current_file)}'...")
                with tarfile.open(output_filename, mode) as tf:
                    tf.add(current_file, arcname=os.path.basename(current_file))
            
            update_task_log(task_id, f"成功: '{os.path.basename(current_file)}' -> '{os.path.basename(output_filename)}'")
            if current_file != original_file:
                update_task_log(task_id, f"日誌: 清理上一層檔案 '{os.path.basename(current_file)}'...")
                os.remove(current_file)
            
            current_file = output_filename
            update_task_progress(task_id, int((i / iterations) * 100))

        tasks_collection.update_one({'_id': task_id}, {'$set': {
            'status': '完成', 'progress': 100, 
            'result_file': os.path.basename(current_file), 
            'password_file_content': password_file_content
        }})
        update_task_log(task_id, "✅ 壓縮流程結束。")
    except Exception as e:
        logging.error(f"壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗'}})
        update_task_log(task_id, f"❌ 嚴重錯誤: {e}")
    finally:
        if os.path.exists(original_file):
            update_task_log(task_id, f"日誌: 清理最初上傳的暫存檔...")
            os.remove(original_file)

# --- 解壓縮背景任務 ---
def decompression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id})
    if not task: return

    params = task['params']
    original_file = params['original_file']
    password_list = params['password_list']
    
    try:
        update_task_log(task_id, f"開始解壓縮檔案: {os.path.basename(original_file)}")
        if not password_list:
            raise ValueError("密碼表是空的，無法進行解壓縮。")

        current_file = original_file
        total_layers = len(password_list)

        for i, layer_info in enumerate(reversed(password_list)):
            layer_num = total_layers - i
            filename, password = layer_info['filename'], layer_info['password']
            update_task_log(task_id, f"--- 第 {layer_num}/{total_layers} 層 ({filename}) ---")
            
            output_path = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_decompress_temp")
            
            if filename.endswith(('.zip', '.7z')):
                update_task_log(task_id, f"日誌: 使用 py7zr 解壓縮 (密碼: {'有' if password else '無'})...")
                with py7zr.SevenZipFile(current_file, 'r', password=password) as z:
                    z.extractall(path=output_path)
            elif any(filename.endswith(ext) for ext in ['.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar']):
                update_task_log(task_id, f"日誌: 使用 tarfile 解壓縮...")
                with tarfile.open(current_file, 'r:*') as tf:
                    tf.extractall(path=output_path)
            else:
                raise ValueError(f"不支援的檔案格式或檔案已損壞: {filename}")
            
            update_task_log(task_id, f"日誌: 成功解開 '{filename}'。")

            if current_file != original_file:
                update_task_log(task_id, f"日誌: 清理上一層檔案 '{os.path.basename(current_file)}'...")
                os.remove(current_file)
            
            extracted_files = os.listdir(output_path)
            if not extracted_files:
                raise ValueError(f"解壓縮後找不到任何檔案。")
            if len(extracted_files) > 1:
                 logging.warning(f"解壓縮後發現多個檔案({len(extracted_files)})，將只處理第一個檔案。")

            extracted_file_path = os.path.join(output_path, extracted_files[0])
            update_task_log(task_id, f"日誌: 找到解出的檔案 '{extracted_files[0]}'")
            
            shutil.move(extracted_file_path, os.path.join(OUTPUT_FOLDER, extracted_files[0]))
            update_task_log(task_id, f"日誌: 清理暫存資料夾 '{output_path}'")
            shutil.rmtree(output_path)
            current_file = os.path.join(OUTPUT_FOLDER, extracted_files[0])
            
            update_task_progress(task_id, int(((i + 1) / total_layers) * 100))

        tasks_collection.update_one({'_id': task_id}, {'$set': {
            'status': '完成', 'progress': 100, 
            'result_file': os.path.basename(current_file)
        }})
        update_task_log(task_id, "✅ 解壓縮流程結束。")
    except (py7zr.Bad7zFile, zipfile.BadZipFile) as bad_file_error:
        logging.error(f"解壓縮任務 {task_id_str} 失敗: 檔案可能已損壞或密碼錯誤 - {bad_file_error}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗'}})
        update_task_log(task_id, f"❌ 錯誤: 檔案可能已損壞或密碼錯誤。請確認密碼表是否正確。")
    except Exception as e:
        logging.error(f"解壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗'}})
        update_task_log(task_id, f"❌ 嚴重錯誤: {e}")
    finally:
        if os.path.exists(original_file):
            update_task_log(task_id, f"日誌: 清理最初上傳的暫存檔...")
            os.remove(original_file)

# --- API 路由 ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/compress', methods=['POST'])
def compress_route():
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    if 'file' not in request.files: return jsonify({'error': '沒有上傳檔案'}), 400
    
    file = request.files['file']
    
    # *** 關鍵修改：不再使用 secure_filename，直接使用原始檔名 ***
    # 這樣才能正確處理中文、日文等非英文字元。
    filename = file.filename
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        params = {
            'original_file': filepath,
            'iterations': int(request.form.get('iterations', 5)),
            'encrypt_odd': request.form.get('encrypt_mode', 'odd') == 'odd',
            'manual_layers': [int(x.strip()) for x in request.form.get('manual_layers', '').split(',') if x.strip()],
            'formats': [x.strip() for x in request.form.get('formats', 'zip,7z,targz').split(',') if x.strip()]
        }
        task = {'type': 'compress', 'status': '處理中', 'progress': 0, 'logs': [f"收到檔案 '{filename}'"], 'params': params, 'created_at': datetime.utcnow()}
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

    # *** 關鍵修改：同樣地，直接使用原始檔名 ***
    filename = file.filename
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        password_text = request.form.get('passwords', '')
        password_list = []
        for line in password_text.strip().split('\n'):
            match = re.search(r'第 \d+ 層 \((.*?)\):\s*(.*)', line)
            if match:
                fname, password = match.groups()
                fname = fname.strip()
                password = password.strip()
                password_list.append({'filename': fname, 'password': None if password == '(無密碼)' else password})
        
        params = {'original_file': filepath, 'password_list': password_list}
        task = {'type': 'decompress', 'status': '處理中', 'progress': 0, 'logs': [f"收到檔案 '{filename}'"], 'params': params, 'created_at': datetime.utcnow()}
        task_id = tasks_collection.insert_one(task).inserted_id
        threading.Thread(target=decompression_worker, args=(str(task_id),)).start()
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/status/<task_id>')
def task_status(task_id):
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    task = tasks_collection.find_one({'_id': ObjectId(task_id)})
    if task:
        response = {k: v for k, v in task.items() if k != '_id'}
        response['_id'] = str(task['_id'])
        return jsonify(response)
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

