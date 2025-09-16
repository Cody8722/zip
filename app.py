import os
import zipfile
import tarfile
import py7zr
import time
import shutil
import string
import random
import threading
from flask import Flask, request, jsonify, send_from_directory, render_template
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import logging

app = Flask(__name__)
# *** 修改：設定日誌格式，讓訊息更清晰 ***
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 設定 ---
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
# *** 新增日誌：確認資料夾是否成功建立 ***
try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    logging.info(f"資料夾 '{UPLOAD_FOLDER}' 和 '{OUTPUT_FOLDER}' 已確認存在。")
except Exception as e:
    logging.error(f"建立資料夾時發生錯誤: {e}")


# --- 資料庫連線 ---
MONGO_URI = os.environ.get('MONGO_URI')
client = None
db = None
tasks_collection = None
db_connection_error = None

try:
    if not MONGO_URI:
        raise ValueError("錯誤：找不到 MONGO_URI 環境變數。請在 Zeabur 中設定。")
    
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logging.info("✅ 成功連線至 MongoDB！")
    
    db = client['compressor_db'] 
    tasks_collection = db['tasks']

except Exception as e:
    db_connection_error = e
    logging.error(f"❌ 無法連線至 MongoDB: {e}")

# --- 通用輔助函式 ---

def generate_password(length=12):
    characters = string.ascii_letters + string.digits + string.punctuation
    return ''.join(random.choice(characters) for i in range(length))

def get_file_type(filename):
    if filename.endswith('.zip'): return 'zip'
    if filename.endswith('.7z'): return '7z'
    if filename.endswith(('.tar.gz', '.tgz')): return 'targz'
    if filename.endswith(('.tar.bz2', '.tbz2')): return 'tarbz2'
    if filename.endswith(('.tar.xz', '.txz')): return 'tarxz'
    if filename.endswith('.tar'): return 'tar'
    return 'unknown'

# --- 背景任務函式 ---

def update_task_log(task_id, message):
    tasks_collection.update_one({'_id': task_id}, {'$push': {'logs': message}})

def update_task_progress(task_id, progress):
    tasks_collection.update_one({'_id': task_id}, {'$set': {'progress': progress}})

def compression_worker(task_id_str):
    # *** 新增日誌 ***
    logging.info(f"背景壓縮任務 {task_id_str} 已啟動。")
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id})
    if not task:
        logging.error(f"找不到任務 {task_id_str}，背景任務終止。")
        return

    params = task['params']
    original_file = params['original_file']
    iterations = params['iterations']
    encrypt_all_odd_layers = params['encrypt_odd']
    layers_to_encrypt = params['manual_layers']
    compression_formats_sequence = params['formats']
    
    output_prefix = 'secure_layer_'
    
    try:
        password_file_content = "--- 壓縮密碼表 ---\n"
        update_task_log(task_id, "已建立密碼表。")

        formats = {'zip':'.zip', '7z':'.7z', 'targz':'.tar.gz', 'tarbz2':'.tar.bz2', 'tarxz':'.tar.xz'}
        current_file_to_compress = original_file

        for i in range(1, iterations + 1):
            current_format_name = compression_formats_sequence[(i - 1) % len(compression_formats_sequence)]
            output_filename = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_{output_prefix}{i}{formats[current_format_name]}")
            
            # *** 新增日誌 ***
            logging.info(f"任務 {task_id_str}: 正在處理第 {i} 層，輸出至 {output_filename}")

            password = None
            password_log_msg = "(無密碼)"
            
            should_encrypt = (encrypt_all_odd_layers and i % 2 != 0) or (not encrypt_all_odd_layers and i in layers_to_encrypt)
            
            if should_encrypt and not current_format_name.startswith('tar'):
                password = generate_password()
                password_log_msg = password
            elif should_encrypt:
                update_task_log(task_id, f"注意：第 {i} 層是 tar 格式，不支援加密。")
                password_log_msg = f"(不支援加密: {current_format_name})"

            password_file_content += f"第 {i} 層 ({os.path.basename(output_filename)}): {password_log_msg}\n"
            update_task_log(task_id, f"--- 第 {i}/{iterations} 次壓縮 (格式: {current_format_name}, 加密: {'是' if password else '否'}) ---")

            if current_format_name in ('zip', '7z'):
                with py7zr.SevenZipFile(output_filename, 'w', password=password) as szf:
                    szf.write(current_file_to_compress, arcname=os.path.basename(current_file_to_compress))
            else: # tar 系列
                mode = 'w:gz' if current_format_name == 'targz' else 'w:bz2' if current_format_name == 'tarbz2' else 'w:xz'
                with tarfile.open(output_filename, mode) as tf:
                    tf.add(current_file_to_compress, arcname=os.path.basename(current_file_to_compress))
            
            update_task_log(task_id, f"成功: '{os.path.basename(current_file_to_compress)}' -> '{os.path.basename(output_filename)}'")
            if current_file_to_compress != original_file:
                os.remove(current_file_to_compress)
            current_file_to_compress = output_filename
            update_task_progress(task_id, int((i / iterations) * 100))

        final_file = os.path.basename(current_file_to_compress)
        tasks_collection.update_one({'_id': task_id}, {'$set': {
            'status': '完成', 'progress': 100, 
            'result_file': final_file, 
            'password_file_content': password_file_content
        }})
        update_task_log(task_id, "✅ 壓縮流程結束。")
        logging.info(f"任務 {task_id_str} 成功完成。")

    except Exception as e:
        logging.error(f"壓縮任務 {task_id_str} 失敗: {e}", exc_info=True) # exc_info=True 會記錄更詳細的錯誤
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗'}})
        update_task_log(task_id, f"❌ 錯誤: {e}")
    finally:
        # *** 新增日誌 ***
        logging.info(f"任務 {task_id_str}: 正在清理暫存檔案 {original_file}...")
        if os.path.exists(original_file):
            os.remove(original_file)
            logging.info(f"已刪除 {original_file}")

# --- API 路由 ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    # *** 新增日誌 ***
    logging.info("收到 /upload 請求...")
    if db is None:
        logging.error("/upload: 資料庫未連線。")
        return jsonify({'error': '資料庫未連線，請檢查伺服器日誌。'}), 500

    if 'file' not in request.files:
        return jsonify({'error': '沒有上傳檔案'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '沒有選擇檔案'}), 400
    
    # *** 新增更詳細的檔案儲存日誌 ***
    try:
        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        logging.info(f"準備將檔案儲存至: {filepath}")
        file.save(filepath)
        logging.info(f"檔案 '{file.filename}' 已成功儲存。")
    except Exception as e:
        logging.error(f"儲存檔案 '{file.filename}' 時發生嚴重錯誤: {e}", exc_info=True)
        return jsonify({'error': '伺服器儲存檔案時發生錯誤。'}), 500


    try:
        iterations = int(request.form.get('iterations', 5))
        encrypt_mode = request.form.get('encrypt_mode', 'odd')
        manual_layers_str = request.form.get('manual_layers', '')
        formats_str = request.form.get('formats', 'zip,7z,targz')

        params = {
            'original_file': filepath,
            'iterations': iterations,
            'encrypt_odd': encrypt_mode == 'odd',
            'manual_layers': {int(x.strip()) for x in manual_layers_str.split(',') if x.strip()},
            'formats': [x.strip() for x in formats_str.split(',') if x.strip()]
        }
        
        task = {
            'status': '處理中',
            'progress': 0,
            'logs': [f"收到檔案 '{file.filename}'，開始處理..."],
            'params': params,
            'created_at': datetime.utcnow()
        }
        result = tasks_collection.insert_one(task)
        task_id = result.inserted_id
        logging.info(f"已在資料庫中建立新任務: {task_id}")

        threading.Thread(target=compression_worker, args=(str(task_id),)).start()
        
        return jsonify({'task_id': str(task_id)})

    except Exception as e:
        logging.error(f"建立壓縮任務時發生錯誤: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 400

@app.route('/status/<task_id>')
def task_status(task_id):
    if db is None:
        return jsonify({'error': '資料庫未連線'}), 500
        
    task = tasks_collection.find_one({'_id': ObjectId(task_id)})
    if task:
        return jsonify({
            'status': task.get('status'),
            'progress': task.get('progress'),
            'logs': task.get('logs'),
            'result_file': task.get('result_file'),
            'password_file_content': task.get('password_file_content')
        })
    return jsonify({'error': '找不到任務'}), 404

@app.route('/download/<path:filename>')
def download_file(filename):
    logging.info(f"收到下載請求: {filename}")
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)

@app.route('/health')
def health_check():
    """新增的健康檢查路由"""
    if db_connection_error:
        return jsonify({
            'status': 'error', 
            'database_connection': 'failed_at_startup', 
            'error': str(db_connection_error)
        }), 500
        
    if client:
        try:
            client.admin.command('ping')
            return jsonify({'status': 'ok', 'database_connection': 'successful'}), 200
        except Exception as e:
            return jsonify({'status': 'error', 'database_connection': 'ping_failed', 'error': str(e)}), 500
    else:
        return jsonify({'status': 'error', 'database_connection': 'not_initialized'}), 500

if __name__ == '__main__':
    # 這部分僅供本地測試使用，在 Zeabur 上會由 gunicorn 啟動
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

