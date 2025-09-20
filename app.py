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
from gridfs import GridFS
from urllib.parse import quote
import qrcode
import io
import secrets
import smtplib
from email.message import EmailMessage

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 設定 ---
UPLOAD_FOLDER = '/tmp/compressor_uploads'
OUTPUT_FOLDER = '/tmp/compressor_outputs'

for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- 資料庫連線 ---
MONGO_URI = os.environ.get('MONGO_URI')
ADMIN_SECRET = os.environ.get('ADMIN_SECRET')
MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')

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

# --- 背景任務 ---
def compression_worker(task_id_str, recipient_email=None, host_url=None):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']
    try:
        iterations = params['iterations']
        # ... (其餘壓縮邏輯與之前相同)
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
    except Exception as e:
        logging.error(f"壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    finally:
        if os.path.exists(original_file): os.remove(original_file)

# *** 關鍵修改：徹底重寫解壓縮邏輯以防止資料遺失 ***
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
            
            if current_file != original_file: os.remove(current_file)
            
            extracted_items = os.listdir(output_path)
            if not extracted_items: raise Exception("解壓縮後找不到任何檔案。")
            
            # 將解壓出的單一檔案/資料夾移出，準備下一輪
            next_item_path = os.path.join(output_path, extracted_items[0])
            moved_item_path = os.path.join(OUTPUT_FOLDER, extracted_items[0])
            shutil.move(next_item_path, moved_item_path)
            shutil.rmtree(output_path) # 清理暫存目錄
            current_file = moved_item_path
            
            update_task_progress(task_id, int(((i + 1) / total_layers) * 100))

        # --- 最終處理：重新打包所有檔案 ---
        update_task_log(task_id, "日誌: 所有層級已解壓，正在重新打包最終檔案...", is_progress_text=True)
        final_filename_to_store = params.get('expected_filename', f"{task_id_str}_decompressed.zip")
        final_archive_path = os.path.join(OUTPUT_FOLDER, final_filename_to_store)

        with zipfile.ZipFile(final_archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            if os.path.isdir(current_file):
                for root, _, files in os.walk(current_file):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, current_file)
                        zipf.write(file_path, arcname)
            else: # 如果解壓出來的是單一檔案
                zipf.write(current_file, os.path.basename(current_file))

        with open(final_archive_path, 'rb') as f_in:
            file_id = fs.put(f_in, filename=final_filename_to_store)

        tasks_collection.update_one({'_id': task_id}, {'$set': {
            'status': '完成', 'progress': 100, 
            'result_file_id': str(file_id), 'result_filename': final_filename_to_store,
            'progress_text': '任務完成！'
        }})
        update_task_log(task_id, "✅ 解壓縮流程結束。")
    except Exception as e:
        logging.error(f"解壓縮任務 {task_id_str} 失敗: {e}", exc_info=True)
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
    finally:
        # 清理所有可能的暫存檔案
        if os.path.exists(original_file): os.remove(original_file)
        if 'current_file' in locals() and os.path.exists(current_file):
            if os.path.isdir(current_file): shutil.rmtree(current_file)
            else: os.remove(current_file)
        if 'final_archive_path' in locals() and os.path.exists(final_archive_path):
             os.remove(final_archive_path)
        if os.path.exists(output_path): shutil.rmtree(output_path)

def send_completion_email(recipient_email, task_id, original_filename, host_url):
    # ... (此函式邏輯不變)
    if not MAIL_USERNAME or not MAIL_PASSWORD: raise Exception("伺服器未設定郵件功能。")
    msg = EmailMessage()
    msg['Subject'] = f"您的檔案「{original_filename}」已壓縮完成！"
    msg['From'] = MAIL_USERNAME
    msg['To'] = recipient_email
    download_url = f"{host_url}download/{task_id}"
    share_url = f"{host_url}?share_id={task_id}"
    html_content = f"""<html><body>...</body></html>""" # 內容省略
    msg.add_alternative(html_content, subtype='html')
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
        smtp.send_message(msg)

# --- API 路由 ---
@app.route('/')
def index(): return render_template('index.html')

# *** 關鍵修改：所有路由增加通用錯誤處理 ***
def handle_route_exception(e, endpoint_name):
    logging.error(f"路由 {endpoint_name} 發生錯誤: {e}", exc_info=True)
    return jsonify({'error': '伺服器內部發生錯誤，請稍後再試。'}), 500

@app.route('/compress', methods=['POST'])
def compress_route():
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        # ... (其餘邏輯與之前相同)
        file = request.files['file']
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
        threading.Thread(target=compression_worker, args=(str(task_id), request.form.get('recipient_email'), request.host_url), daemon=True).start()
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        return handle_route_exception(e, 'compress')

# *** 關鍵修改：使用流式處理讀取檔案 ***
@app.route('/start-shared-decompression/<compress_task_id>', methods=['POST'])
def start_shared_decompression(compress_task_id):
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        original_task = tasks_collection.find_one({'_id': ObjectId(compress_task_id)})
        if not original_task or 'result_file_id' not in original_task: raise ValueError("找不到原始壓縮任務或檔案可能已被刪除。")
        
        filepath = os.path.join(UPLOAD_FOLDER, f"share_{secure_filename(original_task['result_filename'])}")
        grid_out = fs.get(ObjectId(original_task['result_file_id']))
        with open(filepath, 'wb') as f_out:
            for chunk in grid_out: # 流式寫入
                f_out.write(chunk)
        
        params = {
            'original_file': filepath, 'password_list': parse_password_text(original_task.get('password_file_content', '')),
            'master_pass': request.get_json().get('master_password'), 'expected_filename': original_task.get('params', {}).get('raw_filename')
        }
        new_task = {'type': 'decompress', 'status': '處理中', 'params': params, 'created_at': datetime.utcnow()}
        new_task_id = tasks_collection.insert_one(new_task).inserted_id
        tasks_collection.update_one({'_id': new_task_id}, {'$set': {'progress_text': '準備開始...'}})
        threading.Thread(target=decompression_worker, args=(str(new_task_id),)).start()
        return jsonify({'task_id': str(new_task_id)})
    except Exception as e:
        return handle_route_exception(e, 'start_shared_decompression')

# *** 關鍵修改：使用 secrets.compare_digest() ***
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

# --- (其餘路由與之前相同，但都加上了通用錯誤處理) ---
@app.route('/decompress-manual', methods=['POST'])
def decompress_manual_route():
    try:
        # ... 邏輯 ...
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        return handle_route_exception(e, 'decompress_manual')
@app.route('/storage-stats')
def storage_stats():
    try:
        # ... 邏輯 ...
        return jsonify({'used_space': used_space, 'total_space': total_space})
    except Exception as e:
        return handle_route_exception(e, 'storage_stats')
# ...以此類推...

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

