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
from gridfs import Gridfs
from urllib.parse import quote
import qrcode
import io
import secrets
import smtplib # *** 關鍵修改：引入寄信工具 ***
from email.message import EmailMessage # *** 關鍵修改：引入信件格式工具 ***

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
ADMIN_SECRET = os.environ.get('ADMIN_SECRET')
# *** 關鍵修改：讀取郵局資訊 ***
MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')


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
    fs = GridFS(db)
except Exception as e:
    db_connection_error = e
    logging.error(f"❌ 應用程式啟動失敗: {e}")

# --- (通用輔助函式與背景任務邏輯不變) ---
# ... (此處省略與之前版本相同的程式碼以節省篇幅) ...
# --- 背景任務 ---
def compression_worker(task_id_str, recipient_email=None): # *** 關鍵修改：接收 Email 參數 ***
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']
    
    try:
        # ... (壓縮的核心邏輯不變) ...
        
        # *** 關鍵修改：在任務的最後，加入寄信的邏輯 ***
        if recipient_email:
            try:
                send_completion_email(recipient_email, task_id_str, params['raw_filename'])
                update_task_log(task_id, f"✅ 已成功寄送通知信至: {recipient_email}")
            except Exception as e:
                update_task_log(task_id, f"⚠️ 寄送通知信失敗: {e}")

    except Exception as e:
        tasks_collection.update_one({'_id': task_id}, {'$set': {'status': '失敗', 'progress_text': '任務失敗'}})
        update_task_log(task_id, f"❌ 嚴重錯誤: {e}")
    finally:
        if os.path.exists(original_file): os.remove(original_file)

# --- (解壓縮背景任務不變) ---

# *** 關鍵修改：新增寄信函式 ***
def send_completion_email(recipient_email, task_id, original_filename):
    """當壓縮完成後，寄送通知信"""
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        logging.warning("MAIL_USERNAME 或 MAIL_PASSWORD 未設定，無法寄送 Email。")
        raise Exception("伺服器未設定郵件功能。")

    msg = EmailMessage()
    msg['Subject'] = f"您的檔案「{original_filename}」已壓縮完成！"
    msg['From'] = MAIL_USERNAME
    msg['To'] = recipient_email

    # 建立信件內容 (HTML 格式)
    download_url = f"{request.host_url}download/{task_id}"
    share_url = f"{request.host_url}?share_id={task_id}"
    
    html_content = f"""
    <html>
        <body>
            <p>您好，</p>
            <p>您先前提交的檔案 <b>{original_filename}</b> 已經成功壓縮完成了。</p>
            <p>您可以透過以下連結進行操作：</p>
            <ul>
                <li><a href="{download_url}"><b>直接下載壓縮檔</b></a></li>
                <li><a href="{share_url}">產生分享連結與 QR Code</a></li>
            </ul>
            <p>感謝您的使用！</p>
        </body>
    </html>
    """
    msg.add_alternative(html_content, subtype='html')

    # 連線至 Gmail 的 SMTP 伺服器並寄送
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
        smtp.send_message(msg)

# --- API 路由 ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/compress', methods=['POST'])
def compress_route():
    if db is None: return jsonify({'error': '資料庫未連線'}), 500
    if 'file' not in request.files: return jsonify({'error': '沒有上傳檔案'}), 400
    
    file = request.files['file']
    original_filename = file.filename
    safe_filename = secure_filename(file.filename)
    recipient_email = request.form.get('recipient_email') # *** 關鍵修改：接收 Email ***

    try:
        params = {
            'raw_filename': original_filename,
            'iterations': int(request.form.get('iterations', 5)),
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
        
        filepath = os.path.join(UPLOAD_FOLDER, f"{str(task_id)}_{safe_filename}")
        file.save(filepath)
        
        tasks_collection.update_one({'_id': task_id}, {'$set': {'params.original_file': filepath, 'status': '處理中', 'progress_text': '準備開始...'}})
        
        # *** 關鍵修改：將 Email 傳遞給背景任務 ***
        threading.Thread(target=compression_worker, args=(str(task_id), recipient_email), daemon=True).start()
        
        return jsonify({'task_id': str(task_id)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# --- (剩餘所有 API 路由與之前版本完全相同) ---
# ... (此處省略) ...

