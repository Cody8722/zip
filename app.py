import os
import shutil
import string
import time
import uuid
import re
import random
import threading
from datetime import datetime, timedelta

import py7zr
import tarfile
import zipfile

from flask import (Flask, request, jsonify, send_from_directory,
                   render_template)
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# --- App Configuration ---
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

app = Flask(__name__, template_folder='templates')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER

# --- Database Configuration ---
try:
    # 從環境變數讀取 MongoDB 連線字串，這是部署到 Zeabur 的標準做法
    MONGO_URI = os.environ.get('MONGO_URI')
    if not MONGO_URI:
        raise ValueError("錯誤：找不到 MONGO_URI 環境變數。請在 Zeabur 設定中提供。")
    
    client = MongoClient(MONGO_URI)
    # 測試連線
    client.admin.command('ping')
    db = client.get_default_database()
    tasks_collection = db.tasks
    print("✅ 成功連線至 MongoDB。")

    # 為任務文件建立 TTL 索引，讓它們在 1 小時後自動從資料庫刪除
    tasks_collection.create_index("createdAt", expireAfterSeconds=3600)
    print("✅ 已設定 MongoDB TTL 索引 (1 小時後自動清理)。")

except (ValueError, ConnectionFailure) as e:
    print(f"❌ 無法連線至 MongoDB: {e}")
    # 如果無法連線資料庫，程式將無法運作，直接退出
    exit(1)


# --- Helper Functions ---
def generate_password(length=12):
    characters = string.ascii_letters + string.digits + string.punctuation
    return ''.join(random.choice(characters) for i in range(length))

def get_file_type(filename):
    if filename.endswith('.zip'): return 'zip'
    if filename.endswith('.7z'): return '7z'
    if any(filename.endswith(ext) for ext in ['.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar']):
        return 'tar'
    return 'unknown'

# --- Backend Task Logic (Interacting with MongoDB) ---
def update_task_log(task_id, message):
    tasks_collection.update_one({'_id': task_id}, {'$push': {'logs': message}})

def update_task_progress(task_id, current, total):
    tasks_collection.update_one({'_id': task_id}, {'$set': {'progress.current': current, 'progress.total': total}})

def perform_secure_compression(task_id, params):
    try:
        original_file = params['original_file']
        iterations = params['iterations']
        encrypt_all_odd_layers = params['encrypt_odd']
        layers_to_encrypt = params['manual_layers']
        compression_formats_sequence = params['formats']
        delete_original = params['delete_original']
        
        output_dir = app.config['OUTPUT_FOLDER']
        output_prefix = 'secure_layer_'
        # Extract original filename after the initial UUID part
        source_basename = os.path.splitext(os.path.basename(original_file).split('_', 1)[1])[0]
        password_file = os.path.join(output_dir, f'{source_basename}_passwords_{task_id}.txt')

        update_task_log(task_id, f"原始檔案 '{source_basename}' 上傳成功。")
        update_task_log(task_id, "---")

        with open(password_file, 'w', encoding='utf-8') as pf:
            pf.write("--- 壓縮密碼表 ---\n")
        update_task_log(task_id, f"已建立新的密碼表。")

        formats = {'zip':'.zip', '7z':'.7z', 'targz':'.tar.gz', 'tarbz2':'.tar.bz2', 'tarxz':'.tar.xz'}
        current_file_to_compress = original_file
        
        temp_files_to_clean = []

        for i in range(1, iterations + 1):
            update_task_progress(task_id, i, iterations)
            current_format_name = compression_formats_sequence[(i - 1) % len(compression_formats_sequence)]
            output_filename = os.path.join(output_dir, f"{source_basename}_{output_prefix}{i}_{task_id}{formats[current_format_name]}")
            
            password, password_log_msg = None, "(無密碼)"
            should_encrypt = (encrypt_all_odd_layers and i % 2 != 0) or (not encrypt_all_odd_layers and i in layers_to_encrypt)
            
            if should_encrypt and not current_format_name.startswith('tar'):
                password = generate_password()
                password_log_msg = password
            elif should_encrypt:
                update_task_log(task_id, f"注意：第 {i} 層是 tar 格式，不支援加密。")
                password_log_msg = f"(不支援加密: {current_format_name})"
            
            with open(password_file, 'a', encoding='utf-8') as pf:
                pf.write(f"第 {i} 層 ({os.path.basename(output_filename)}): {password_log_msg}\n")
            
            update_task_log(task_id, f"--- 第 {i}/{iterations} 次壓縮 (格式: {current_format_name}, 加密: {'是' if password else '否'}) ---")

            if current_format_name in ('zip', '7z'):
                with py7zr.SevenZipFile(output_filename, 'w', password=password) as szf:
                    szf.write(current_file_to_compress, arcname=os.path.basename(source_basename))
            else: # tar
                mode = 'w:gz' if current_format_name == 'targz' else 'w:bz2' if current_format_name == 'tarbz2' else 'w:xz'
                with tarfile.open(output_filename, mode) as tf:
                    tf.add(current_file_to_compress, arcname=os.path.basename(source_basename))
            
            update_task_log(task_id, f"成功: '{os.path.basename(current_file_to_compress)}' -> '{os.path.basename(output_filename)}'")
            
            if current_file_to_compress != original_file:
                os.remove(current_file_to_compress)
            
            current_file_to_compress = output_filename
            temp_files_to_clean.append(output_filename)

        final_product = current_file_to_compress
        final_zip_name = f'result_{source_basename}_{task_id}.zip'
        final_zip_path = os.path.join(output_dir, final_zip_name)
        with zipfile.ZipFile(final_zip_path, 'w') as zf:
            zf.write(final_product, arcname=os.path.basename(final_product))
            zf.write(password_file, arcname=os.path.basename(password_file))

        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': 'completed', 'output_file': final_zip_name}})
        update_task_log(task_id, f"\n✅ 壓縮流程結束。")
        update_task_log(task_id, f"最終結果已打包為 '{final_zip_name}'。")

        if delete_original:
            os.remove(original_file)
        os.remove(password_file)
        for f in temp_files_to_clean:
             os.remove(f)

    except Exception as e:
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': 'failed'}})
        update_task_log(task_id, f"\n❌ 錯誤: {e}")

def perform_secure_decompression(task_id, params):
    try:
        file_to_decompress = params['file_to_decompress']
        password_file = params['password_file']
        
        update_task_log(task_id, "正在讀取密碼表...")
        passwords = {}
        with open(password_file, 'r', encoding='utf-8') as pf:
            for line in pf:
                match = re.search(r"第 (\d+) 層 \((.*?)\): (.*)", line)
                if match:
                    layer_num, filename, password = match.groups()
                    passwords[filename] = None if password == "(無密碼)" or password.startswith("(不支援") else password
        update_task_log(task_id, "密碼表讀取完畢。\n")

        target_dir = app.config['OUTPUT_FOLDER']
        current_file = file_to_decompress
        
        # Improved regex to find layer number
        match = re.search(r'_layer_(\d+)_', os.path.basename(current_file)) or re.search(r'(\d+)', os.path.basename(current_file))
        total_layers = int(match.group(1)) if match else 1

        for i in range(total_layers):
            update_task_progress(task_id, i + 1, total_layers)
            file_type = get_file_type(current_file)
            if file_type == 'unknown':
                update_task_log(task_id, f"\n✅ 解壓縮完成！")
                break
            
            layer_num = total_layers - i
            update_task_log(task_id, f"--- 正在解第 {layer_num} 層: '{os.path.basename(current_file)}' ---")
            
            password = passwords.get(os.path.basename(current_file))
            update_task_log(task_id, f"使用密碼: {'是' if password else '否'}")

            output_temp_dir = os.path.join(target_dir, f"decompress_temp_{task_id}")
            if os.path.exists(output_temp_dir): shutil.rmtree(output_temp_dir)
            os.makedirs(output_temp_dir)
            
            if file_type in ('zip', '7z'):
                with py7zr.SevenZipFile(current_file, 'r', password=password) as szf:
                    szf.extractall(path=output_temp_dir)
            else: # tar
                with tarfile.open(current_file, 'r:*') as tf:
                    tf.extractall(path=output_temp_dir)
            
            extracted_filename = os.listdir(output_temp_dir)[0]
            update_task_log(task_id, f"成功解出: '{extracted_filename}'")
            
            # Prepend task_id to avoid name collision in the shared output folder
            new_file_path = os.path.join(target_dir, f"{task_id}_{extracted_filename}")
            os.rename(os.path.join(output_temp_dir, extracted_filename), new_file_path)
            
            os.remove(current_file)
            shutil.rmtree(output_temp_dir)
            current_file = new_file_path
        
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': 'completed', 'output_file': os.path.basename(current_file)}})

    except Exception as e:
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': 'failed'}})
        update_task_log(task_id, f"\n❌ 錯誤: {e}")

# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/encode', methods=['POST'])
def encode_route():
    if 'source_file' not in request.files: return jsonify({'error': '沒有上傳檔案'}), 400
    file = request.files['source_file']
    if file.filename == '': return jsonify({'error': '沒有選擇檔案'}), 400
    
    filename = str(uuid.uuid4()) + "_" + file.filename
    original_file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(original_file_path)
    
    task_id = str(uuid.uuid4())
    params = {
        'original_file': original_file_path,
        'iterations': int(request.form.get('iterations', 5)),
        'encrypt_odd': request.form.get('encrypt_mode') == 'odd',
        'manual_layers': {int(x.strip()) for x in request.form.get('manual_layers', '').split(',') if x.strip()},
        'formats': [x.strip() for x in request.form.get('formats', 'zip,7z,targz').split(',') if x.strip()],
        'delete_original': request.form.get('delete_original') == 'on'
    }
    
    task_document = {
        '_id': task_id,
        'status': 'processing', 
        'logs': [], 
        'progress': {'current': 0, 'total': params['iterations']},
        'createdAt': datetime.utcnow()
    }
    tasks_collection.insert_one(task_document)
    
    thread = threading.Thread(target=perform_secure_compression, args=(task_id, params))
    thread.start()
    
    return jsonify({'task_id': task_id})

@app.route('/decode', methods=['POST'])
def decode_route():
    if 'compressed_file' not in request.files or 'password_file' not in request.files:
        return jsonify({'error': '缺少壓縮檔或密碼表'}), 400
    
    compressed_file = request.files['compressed_file']
    password_file = request.files['password_file']

    comp_filename = str(uuid.uuid4()) + "_" + compressed_file.filename
    comp_file_path = os.path.join(app.config['UPLOAD_FOLDER'], comp_filename)
    compressed_file.save(comp_file_path)
    
    pass_filename = str(uuid.uuid4()) + "_" + password_file.filename
    pass_file_path = os.path.join(app.config['UPLOAD_FOLDER'], pass_filename)
    password_file.save(pass_file_path)

    task_id = str(uuid.uuid4())
    params = {
        'file_to_decompress': comp_file_path,
        'password_file': pass_file_path,
    }
    
    task_document = {
        '_id': task_id,
        'status': 'processing', 
        'logs': [], 
        'progress': {'current': 0, 'total': 1},
        'createdAt': datetime.utcnow()
    }
    tasks_collection.insert_one(task_document)

    thread = threading.Thread(target=perform_secure_decompression, args=(task_id, params))
    thread.start()

    return jsonify({'task_id': task_id})

@app.route('/status/<task_id>')
def status_route(task_id):
    task = tasks_collection.find_one({'_id': task_id})
    if not task:
        return jsonify({'status': 'not_found'}), 404
    # The _id is already a string, no need for conversion
    return jsonify(task)

@app.route('/download/<task_id>')
def download_route(task_id):
    task = tasks_collection.find_one({'_id': task_id})
    if not task or task.get('status') != 'completed':
        return "檔案尚未準備好或任務不存在", 404
    
    return send_from_directory(app.config['OUTPUT_FOLDER'], task['output_file'], as_attachment=True)

# --- Cleanup Job ---
def cleanup_old_files():
    """定期清理 uploads 和 outputs 資料夾中超過 1 小時的舊檔案"""
    while True:
        now = time.time()
        cutoff = now - 3600  # 1 小時前

        for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path):
                    if os.path.getmtime(file_path) < cutoff:
                        try:
                            os.remove(file_path)
                            print(f"已清理過期檔案: {file_path}")
                        except OSError as e:
                            print(f"清理檔案時發生錯誤 {file_path}: {e}")
        
        time.sleep(600) # 每 10 分鐘檢查一次

if __name__ == '__main__':
    # 在背景啟動清理執行緒
    cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
    cleanup_thread.start()
    # 讓 Flask 在所有網路介面上監聽，這是 Zeabur 部署所必需的
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

