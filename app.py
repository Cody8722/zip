import os
import zipfile
import tarfile
import py7zr
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
from datetime import datetime
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
from cryptography.fernet import Fernet
import redis
import json
from typing import Optional, Dict, Any, Tuple, List

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # 禁用靜態文件緩存以減少記憶體佔用
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 設定 ---
UPLOAD_FOLDER = '/tmp/compressor_uploads'
OUTPUT_FOLDER = '/tmp/compressor_outputs'
for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- 資料庫與環境變數 ---
MONGO_URI = os.environ.get('MONGO_URI')
REDIS_URL = os.environ.get('REDIS_URL')  # Redis 快取 URL (選填)
ADMIN_SECRET = os.environ.get('ADMIN_SECRET')
MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
PASSWORD_ENCRYPTION_KEY = os.environ.get('PASSWORD_ENCRYPTION_KEY')  # Fernet 加密密鑰

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
CANCEL_CHECK_INTERVAL = 1  # 每 1 層檢查一次取消狀態，提高取消響應速度
FILENAME_UNIQUE_SUFFIX_BYTES = 2  # 檔名唯一性後綴長度（2 bytes = 4 個字符）
TASK_SALT_BYTES = 16  # 任務鹽值長度（16 bytes = 32 個字符）
DELETE_TOKEN_BYTES = 16  # 刪除令牌長度（16 bytes = 32 個字符）
RANDOM_FILENAME_BYTES = 8  # 隨機檔名長度（8 bytes = 16 個字符）

# 清理標誌位（防止雙重清理）
_cleanup_executed = False

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

# --- Redis 快取（選填） ---
redis_client = None
CACHE_TASK_TTL = 60  # 任務快取過期時間（秒）
try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3)
        redis_client.ping()
        logging.info("✅ 成功連線至 Redis！")
    else:
        logging.info("ℹ️  未設置 REDIS_URL，將不使用快取功能")
except Exception as e:
    logging.warning(f"⚠️  Redis 連線失敗，將繼續運行但不使用快取: {e}")
    redis_client = None

# --- 清理函數 ---
def cleanup_stale_files():
    """清理臨時目錄中的殘留檔案"""
    try:
        for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
            if not os.path.exists(folder):
                continue

            file_count = 0
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                try:
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                        file_count += 1
                    elif os.path.isdir(filepath):
                        shutil.rmtree(filepath)
                        file_count += 1
                except Exception as e:
                    logging.warning(f"清理檔案 {filepath} 失敗: {e}")

            if file_count > 0:
                logging.info(f"✅ 已清理 {folder} 中的 {file_count} 個殘留檔案")
    except Exception as e:
        logging.error(f"清理臨時檔案時發生錯誤: {e}")

def cleanup_stale_tasks():
    """清理資料庫中卡住的任務（超過 2 小時仍在處理中）"""
    try:
        if tasks_collection is None:
            return

        from datetime import timedelta
        cutoff_time = datetime.utcnow() - timedelta(hours=2)

        result = tasks_collection.update_many(
            {
                'status': {'$in': ['處理中', 'pending']},
                'created_at': {'$lt': cutoff_time}
            },
            {
                '$set': {
                    'status': '失敗',
                    'progress_text': '任務超時（系統重啟或崩潰）'
                }
            }
        )

        if result.modified_count > 0:
            logging.info(f"✅ 已將 {result.modified_count} 個卡住的任務標記為失敗")
    except Exception as e:
        logging.error(f"清理卡住的任務時發生錯誤: {e}")

def cleanup_on_startup():
    """應用程式啟動時執行清理"""
    logging.info("🔧 執行啟動清理...")
    cleanup_stale_tasks()
    cleanup_stale_files()

    # 重置活動任務計數器
    global active_task_count
    active_task_count = 0
    logging.info("✅ 啟動清理完成")

def cleanup_on_exit():
    """應用程式退出時清理資源"""
    global _cleanup_executed

    # 防止重複執行（atexit 和 signal handler 都會調用）
    if _cleanup_executed:
        logging.info("⏭️ 清理已執行過，跳過")
        return

    _cleanup_executed = True
    logging.info("🔧 正在清理資源...")

    try:
        # 標記所有處理中的任務為中斷
        if tasks_collection is not None:
            result = tasks_collection.update_many(
                {'status': {'$in': ['處理中', 'pending']}},
                {'$set': {'status': '已取消', 'progress_text': '伺服器關閉'}}
            )
            if result.modified_count > 0:
                logging.info(f"✅ 已標記 {result.modified_count} 個任務為已取消")
    except Exception as e:
        logging.error(f"標記任務為已取消時發生錯誤: {e}")

    try:
        # 等待線程池關閉（最多等待 30 秒）
        executor.shutdown(wait=True)
        logging.info("✅ 線程池已清理")
    except Exception as e:
        logging.error(f"清理線程池時發生錯誤: {e}")

    try:
        # 關閉 MongoDB 連接
        if client is not None:
            client.close()
            logging.info("✅ MongoDB 連接已關閉")
    except Exception as e:
        logging.error(f"關閉 MongoDB 連接時發生錯誤: {e}")

    try:
        # 關閉 Redis 連接
        if redis_client is not None:
            redis_client.close()
            logging.info("✅ Redis 連接已關閉")
    except Exception as e:
        logging.error(f"關閉 Redis 連接時發生錯誤: {e}")

# 註冊清理函數
atexit.register(cleanup_on_exit)

# 處理信號（Ctrl+C 或 Docker 重啟）
def signal_handler(signum, frame):
    signal_name = 'SIGTERM' if signum == signal.SIGTERM else 'SIGINT'
    logging.info(f"⚠️ 收到 {signal_name} 信號，正在優雅地關閉...")
    cleanup_on_exit()
    exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# 定期清理背景任務
def periodic_cleanup():
    """每小時執行一次清理，防止檔案堆積"""
    import time
    while True:
        try:
            time.sleep(3600)  # 每 1 小時
            logging.info("🔧 執行定期清理...")
            cleanup_stale_tasks()
            cleanup_stale_files()
        except Exception as e:
            logging.error(f"定期清理時發生錯誤: {e}")

# 啟動定期清理線程（守護線程，不會阻止程式退出）
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

# 執行啟動清理
cleanup_on_startup()

# --- 通用輔助函式 ---
def calculate_encrypt_layers(
    encrypt_mode: str,
    iterations: int,
    manual_layers: Optional[List[int]] = None,
    multiple_interval: int = 3,
    arithmetic_start: int = 1,
    arithmetic_diff: int = 2
) -> set:
    """
    根據加密模式計算需要加密的層數列表

    Args:
        encrypt_mode: 加密模式 ('none', 'all', 'odd', 'even', 'multiple', 'arithmetic', 'manual')
        iterations: 總層數
        manual_layers: 手動指定的層數列表
        multiple_interval: 倍數間隔
        arithmetic_start: 等差數列首項
        arithmetic_diff: 等差數列公差

    Returns:
        set: 需要加密的層數集合
    """
    if encrypt_mode == 'none':
        return set()
    elif encrypt_mode == 'all':
        return set(range(1, iterations + 1))
    elif encrypt_mode == 'odd':
        return {i for i in range(1, iterations + 1) if i % 2 != 0}
    elif encrypt_mode == 'even':
        return {i for i in range(1, iterations + 1) if i % 2 == 0}
    elif encrypt_mode == 'multiple':
        return {i for i in range(multiple_interval, iterations + 1, multiple_interval)}
    elif encrypt_mode == 'arithmetic':
        layers = set()
        current = arithmetic_start
        while current <= iterations:
            layers.add(current)
            current += arithmetic_diff
        return layers
    elif encrypt_mode == 'manual':
        return set(manual_layers) if manual_layers else set()
    else:
        return set()

def validate_object_id(id_string: str) -> Tuple[Optional[ObjectId], Optional[str]]:
    """
    驗證 ObjectId 字符串格式是否有效

    參數:
        id_string: 要驗證的字符串

    返回:
        (ObjectId, None) 如果有效
        (None, error_message) 如果無效
    """
    try:
        return ObjectId(id_string), None
    except Exception:
        return None, '無效的任務 ID 格式'


def generate_password(filename: str, salt: str, length: int = 16) -> str:
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


def get_cipher() -> Optional[Fernet]:
    """
    獲取 Fernet 加密器實例

    Returns:
        Fernet 加密器，如果密鑰未設置則返回 None
    """
    if not PASSWORD_ENCRYPTION_KEY:
        logging.warning("⚠️ PASSWORD_ENCRYPTION_KEY 未設置，密碼將以元數據形式存儲（不加密）")
        return None
    try:
        return Fernet(PASSWORD_ENCRYPTION_KEY.encode() if isinstance(PASSWORD_ENCRYPTION_KEY, str) else PASSWORD_ENCRYPTION_KEY)
    except Exception as e:
        logging.error(f"❌ 無法初始化加密器: {e}")
        return None


def encrypt_password(password: str) -> Optional[str]:
    """
    加密密碼

    Args:
        password: 明文密碼字串

    Returns:
        加密後的密碼（Base64 字串），如果加密失敗則返回 None
    """
    if not password:
        return None

    cipher = get_cipher()
    if not cipher:
        # 如果沒有加密密鑰，返回 None（將使用元數據方式）
        return None

    try:
        encrypted = cipher.encrypt(password.encode('utf-8'))
        return base64.urlsafe_b64encode(encrypted).decode('utf-8')
    except Exception as e:
        logging.error(f"❌ 加密密碼失敗: {e}")
        return None


def decrypt_password(encrypted_password: str) -> Optional[str]:
    """
    解密密碼

    Args:
        encrypted_password: 加密的密碼字串（Base64）

    Returns:
        解密後的明文密碼，如果解密失敗則返回 None
    """
    if not encrypted_password:
        return None

    cipher = get_cipher()
    if not cipher:
        logging.error("❌ 無法解密：未設置加密密鑰")
        return None

    try:
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_password.encode('utf-8'))
        decrypted = cipher.decrypt(encrypted_bytes)
        return decrypted.decode('utf-8')
    except Exception as e:
        logging.error(f"❌ 解密密碼失敗: {e}")
        return None


def regenerate_passwords_from_metadata(metadata: Dict[str, Any], master_pass: Optional[str] = None) -> Optional[str]:
    """
    從元數據重新生成密碼文件內容

    Args:
        metadata: 包含 task_salt 和 layers 的字典
        master_pass: 特殊密碼（如果有）

    Returns:
        密碼文件內容字串
    """
    if not metadata or 'task_salt' not in metadata or 'layers' not in metadata:
        return None

    task_salt = metadata['task_salt']
    layers = metadata['layers']

    content = "--- 壓縮密碼表 ---\n"
    content += f"# 任務鹽值 (Salt): {task_salt}\n"

    for layer in layers:
        layer_num = layer['num']
        filename = layer['filename']
        has_password = layer.get('has_password', False)
        is_master = layer.get('is_master', False)
        pwd_length = layer.get('length', 16)

        if not has_password:
            log_pwd = "(無密碼)"
        elif is_master:
            if master_pass:
                log_pwd = "(特殊密碼層)"
            else:
                log_pwd = "(特殊密碼層 - 需要您設定的特殊密碼才能解壓)"
        else:
            # 優先嘗試從加密密碼解密
            encrypted_pwd = layer.get('encrypted_password')
            if encrypted_pwd:
                # 新版本：使用加密存儲的密碼
                password = decrypt_password(encrypted_pwd)
                if password:
                    log_pwd = f"{password} (長度: {len(password)}, 已加密)"
                else:
                    log_pwd = "(密碼解密失敗 - 請檢查加密密鑰)"
            else:
                # 舊版本：從元數據重新生成密碼
                password = generate_password(filename, task_salt, pwd_length)
                log_pwd = f"{password} (長度: {pwd_length})"

        content += f"第 {layer_num} 層 ({filename}): {log_pwd}\n"

    return content

def safe_db_operation(operation_func, operation_name: str = "資料庫操作") -> Any:
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

def update_task_log(task_id: ObjectId, message: str, is_progress_text: bool = False) -> None:
    def _update():
        update_doc = {'$push': {'logs': message}}
        if is_progress_text:
            update_doc['$set'] = {'progress_text': message}
        result = tasks_collection.update_one({'_id': task_id}, update_doc)

        # 讓快取失效，確保下次查詢時能讀到最新的 logs
        # logs 是陣列，難以在 Redis 中即時更新，所以選擇刪除快取
        if redis_client:
            try:
                cache_key = f"task:{task_id}"
                redis_client.delete(cache_key)
                # logging.debug(f"已清除任務 {task_id} 的快取，確保日誌即時更新")
            except Exception as e:
                logging.warning(f"⚠️ 清除快取失敗: {e}")

        return result
    safe_db_operation(_update, f"更新任務日誌 ({task_id})")

def update_task_progress(task_id: ObjectId, progress: int) -> None:
    def _update():
        result = tasks_collection.update_one({'_id': task_id}, {'$set': {'progress': progress}})
        # 更新快取中的進度
        if redis_client:
            try:
                cache_key = f"task:{task_id}"
                redis_client.hset(cache_key, 'progress', progress)
            except Exception as e:
                logging.warning(f"⚠️ 更新快取失敗: {e}")
        return result
    safe_db_operation(_update, f"更新任務進度 ({task_id})")


def get_cached_task(task_id: ObjectId) -> Optional[Dict[str, Any]]:
    """
    從快取獲取任務狀態（如果啟用 Redis）

    Args:
        task_id: 任務 ID

    Returns:
        任務數據字典，如果不在快取中則返回 None
    """
    if not redis_client:
        return None

    try:
        cache_key = f"task:{task_id}"
        cached_data = redis_client.hgetall(cache_key)
        if cached_data:
            # 反序列化 JSON 字段
            if 'logs' in cached_data:
                cached_data['logs'] = json.loads(cached_data['logs'])
            if 'params' in cached_data:
                cached_data['params'] = json.loads(cached_data['params'])
            if 'password_metadata' in cached_data:
                cached_data['password_metadata'] = json.loads(cached_data['password_metadata'])
            # 轉換數字類型
            if 'progress' in cached_data:
                cached_data['progress'] = int(cached_data['progress'])
            return cached_data
    except Exception as e:
        logging.warning(f"⚠️ 讀取快取失敗: {e}")

    return None


def cache_task(task_id: ObjectId, task_data: Dict[str, Any]) -> None:
    """
    將任務狀態存入快取

    Args:
        task_id: 任務 ID
        task_data: 任務數據字典
    """
    if not redis_client:
        return

    try:
        cache_key = f"task:{task_id}"
        # 準備快取數據（序列化複雜類型）
        cache_data = {}
        for key, value in task_data.items():
            if key == '_id':
                cache_data['_id'] = str(value)
            elif isinstance(value, (list, dict)):
                cache_data[key] = json.dumps(value, ensure_ascii=False)
            else:
                cache_data[key] = str(value) if value is not None else ''

        # 存入 Redis (使用 hash)
        redis_client.hset(cache_key, mapping=cache_data)
        # 設置過期時間
        redis_client.expire(cache_key, CACHE_TASK_TTL)
    except Exception as e:
        logging.warning(f"⚠️ 寫入快取失敗: {e}")


def invalidate_task_cache(task_id: ObjectId) -> None:
    """
    清除任務快取

    Args:
        task_id: 任務 ID
    """
    if not redis_client:
        return

    try:
        cache_key = f"task:{task_id}"
        redis_client.delete(cache_key)
    except Exception as e:
        logging.warning(f"⚠️ 清除快取失敗: {e}")


def parse_password_text(password_text: str) -> List[Dict[str, Optional[str]]]:
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

def validate_compression_params(params: Dict[str, Any]) -> None:
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

    # 驗證加密模式參數
    encrypt_mode = params.get('encrypt_mode', 'none')
    if encrypt_mode == 'manual':
        manual_layers = params.get('manual_layers', [])
        for layer in manual_layers:
            if not isinstance(layer, int) or layer < 1 or layer > iterations:
                raise ValueError(f"手動設定的密碼層 {layer} 超出有效範圍（1-{iterations}）。")
    elif encrypt_mode == 'multiple':
        multiple_interval = params.get('multiple_interval', 0)
        if not isinstance(multiple_interval, int) or multiple_interval < 2:
            raise ValueError("倍數間隔必須至少為 2。")
    elif encrypt_mode == 'arithmetic':
        arithmetic_start = params.get('arithmetic_start', 0)
        arithmetic_diff = params.get('arithmetic_diff', 0)
        if not isinstance(arithmetic_start, int) or arithmetic_start < 1 or arithmetic_start > iterations:
            raise ValueError(f"等差數列首項必須在 1 到 {iterations} 之間。")
        if not isinstance(arithmetic_diff, int) or arithmetic_diff < 1:
            raise ValueError("等差數列公差必須至少為 1。")

    # 驗證自訂密碼長度配置
    if params.get('use_custom_length'):
        default_length = params.get('default_password_length', 16)
        if not isinstance(default_length, int) or default_length < 8 or default_length > 64:
            raise ValueError("預設密碼長度必須在 8 到 64 之間。")

        password_length_config = params.get('password_length_config', {})
        for layer, length in password_length_config.items():
            if not isinstance(layer, int) or layer < 1 or layer > iterations:
                raise ValueError(f"密碼長度配置中的層數 {layer} 超出有效範圍（1-{iterations}）。")
            if not isinstance(length, int) or length < 8 or length > 64:
                raise ValueError(f"第 {layer} 層的密碼長度必須在 8 到 64 之間。")

    # 驗證 formats
    if not params.get('formats') or len(params['formats']) == 0:
        raise ValueError("至少需要選擇一種壓縮格式。")

def validate_file(file, mode: str = 'compress') -> None:
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


# ============================================================================
# 壓縮輔助函數（Helper Functions for Compression）
# ============================================================================

def _load_task_data(task_id_str: str) -> Optional[Tuple[ObjectId, Dict[str, Any], Dict[str, Any], str, int]]:
    """
    加載任務數據和原始文件信息

    參數:
        task_id_str: 任務 ID 字符串

    返回:
        成功: (task_id, task, params, original_file, original_size)
        失敗: None
    """
    try:
        task_id = ObjectId(task_id_str)
        task = tasks_collection.find_one({'_id': task_id})

        if not task:
            logging.warning(f"任務 {task_id_str} 不存在")
            return None

        params = task['params']
        original_file = params['original_file']

        if not os.path.exists(original_file):
            logging.error(f"原始文件不存在: {original_file}")
            return None

        original_size = os.path.getsize(original_file)

        return (task_id, task, params, original_file, original_size)

    except Exception as e:
        logging.error(f"加載任務數據失敗: {e}", exc_info=True)
        return None


def _initialize_compression(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any], set]:
    """
    初始化壓縮參數（鹽值、元數據結構、加密層計算）

    參數:
        params: 任務參數字典

    返回:
        (task_salt, password_metadata, encrypt_layers)
    """
    # 生成任務鹽值
    task_salt = secrets.token_hex(TASK_SALT_BYTES)

    # 初始化密碼元數據結構
    password_metadata = {
        'task_salt': task_salt,
        'layers': []
    }

    # 計算需要加密的層數
    encrypt_layers = calculate_encrypt_layers(
        params['encrypt_mode'],
        params['iterations'],
        params.get('manual_layers'),
        params.get('multiple_interval', 3),
        params.get('arithmetic_start', 1),
        params.get('arithmetic_diff', 2)
    )

    logging.info(f"加密模式: {params['encrypt_mode']}, 加密層數: {sorted(encrypt_layers)}")

    return (task_salt, password_metadata, encrypt_layers)


def _generate_layer_password(
    layer_num: int,
    params: Dict[str, Any],
    encrypt_layers: set,
    filename_for_password: str,
    task_salt: str,
    format_name: str
) -> Tuple[Optional[str], bool, bool, int]:
    """
    生成層密碼（根據加密模式和配置）

    參數:
        layer_num: 當前層數
        params: 任務參數
        encrypt_layers: 需要加密的層集合
        filename_for_password: 用於生成密碼的文件名
        task_salt: 任務鹽值
        format_name: 壓縮格式名稱

    返回:
        (password, has_password, is_master, pwd_length)
    """
    password = None
    has_password = False
    is_master = False
    pwd_length = 16

    # 優先檢查特殊密碼（主密碼）
    if params['use_master_pass'] and layer_num % params['master_pass_interval'] == 0:
        password = params['master_pass']
        has_password = True
        is_master = True
    # 然後檢查是否在加密層數列表中
    elif layer_num in encrypt_layers:
        if format_name in ('zip', '7z'):
            has_password = True

            # 確定此層的密碼長度
            if params.get('use_custom_length'):
                pwd_length = params['password_length_config'].get(
                    layer_num,
                    params['default_password_length']
                )
            else:
                pwd_length = 16  # 默認長度

            # 使用檔名和任務鹽生成 SHA-256 密碼
            password = generate_password(filename_for_password, task_salt, pwd_length)

    return (password, has_password, is_master, pwd_length)


def _process_compression_layer(
    current_file: str,
    output_filename: str,
    format_name: str,
    password: Optional[str]
) -> None:
    """
    處理單層壓縮邏輯

    參數:
        current_file: 當前要壓縮的文件路徑
        output_filename: 輸出文件路徑
        format_name: 壓縮格式名稱 ('zip', '7z', 'targz')
        password: 加密密碼（None 表示不加密）
    """
    if format_name in ('zip', '7z'):
        # py7zr 支持多線程壓縮
        with py7zr.SevenZipFile(output_filename, 'w', password=password, mp=True) as z:
            z.write(current_file, os.path.basename(current_file))
    else:
        # tarfile 不支持多線程，但可以使用壓縮等級
        with tarfile.open(output_filename, 'w:gz', compresslevel=6) as tf:
            tf.add(current_file, arcname=os.path.basename(current_file))


def _finalize_compression(
    task_id: ObjectId,
    current_file: str,
    original_size: int,
    password_metadata: Dict[str, Any],
    recipient_email: Optional[str],
    host_url: Optional[str],
    task_id_str: str,
    raw_filename: str
) -> None:
    """
    完成壓縮任務（上傳文件、更新狀態、發送郵件）

    參數:
        task_id: 任務 ObjectId
        current_file: 最終壓縮文件路徑
        original_size: 原始文件大小（bytes）
        password_metadata: 密碼元數據字典
        recipient_email: 收件人郵箱（可選）
        host_url: 主機 URL（可選）
        task_id_str: 任務 ID 字符串
        raw_filename: 原始文件名
    """
    # 計算壓縮率
    final_size = os.path.getsize(current_file)
    compression_ratio = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0
    update_task_log(task_id, f"📊 壓縮率: {compression_ratio:.2f}% (原始: {original_size/1024:.2f} KB → 壓縮後: {final_size/1024:.2f} KB)")

    # 上傳到雲端儲存
    update_task_log(task_id, "☁️ 正在上傳至雲端儲存...", is_progress_text=True)
    with open(current_file, 'rb') as f_in:
        file_id = fs.put(f_in, filename=os.path.basename(current_file))
    os.remove(current_file)
    update_task_log(task_id, "✅ 檔案已上傳至雲端儲存")

    # 生成安全令牌
    delete_token = secrets.token_hex(DELETE_TOKEN_BYTES)

    # 儲存任務結果
    update_task_log(task_id, "💾 正在儲存任務資訊...", is_progress_text=True)
    tasks_collection.update_one({'_id': task_id}, {'$set': {
        'status': '完成', 'progress': 100,
        'result_file_id': str(file_id),
        'result_filename': os.path.basename(current_file),
        'password_metadata': password_metadata,
        'delete_token': delete_token,
        'original_size': original_size,
        'final_size': final_size,
        'compression_ratio': round(compression_ratio, 2)
    }})

    # 發送郵件通知
    if recipient_email and host_url:
        try:
            send_completion_email(recipient_email, task_id_str, raw_filename, host_url)
            update_task_log(task_id, f"✅ 已成功寄送通知信至: {recipient_email}")
        except Exception as e:
            update_task_log(task_id, f"⚠️ 寄送通知信失敗: {e}")


def _cleanup_files(files_to_clean: List[str]) -> None:
    """
    清理臨時文件

    參數:
        files_to_clean: 要清理的文件路徑列表
    """
    for temp_file in files_to_clean:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
                logging.info(f"已清理文件: {temp_file}")
            except Exception as e:
                logging.error(f"清理文件失敗 {temp_file}: {e}")


def compression_worker(task_id_str, recipient_email=None, host_url=None):
    # 加載任務數據
    task_data = _load_task_data(task_id_str)
    if not task_data:
        return

    task_id, task, params, original_file, original_size = task_data

    # 追蹤所有生成的檔案以便失敗時清理
    generated_files = []

    try:
        iterations = params['iterations']

        # 初始化壓縮參數
        task_salt, password_metadata, encrypt_layers = _initialize_compression(params)

        formats = {'zip': '.zip', '7z': '.7z', 'targz': '.tar.gz'}
        current_file = original_file

        # 處理每一層壓縮
        for i in range(1, iterations + 1):
            # 檢查取消狀態
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
                _cleanup_files(generated_files)
                return

            # 確定格式和文件名
            format_name = params['formats'][(i - 1) % len(params['formats'])]

            if i == iterations:
                # 最後一層：使用原始檔名
                safe_raw_filename = secure_filename(params['raw_filename'])
                base_name = os.path.splitext(safe_raw_filename)[0]
                unique_suffix = secrets.token_hex(FILENAME_UNIQUE_SUFFIX_BYTES)
                final_filename = f"{base_name}_{unique_suffix}{formats[format_name]}"
                output_filename = os.path.join(OUTPUT_FOLDER, final_filename)
                filename_for_password = final_filename
            else:
                # 中間層：使用隨機檔名
                random_filename = secrets.token_hex(RANDOM_FILENAME_BYTES) + formats[format_name]
                output_filename = os.path.join(OUTPUT_FOLDER, random_filename)
                filename_for_password = random_filename

            # 生成層密碼
            password, has_password, is_master, pwd_length = _generate_layer_password(
                i, params, encrypt_layers, filename_for_password, task_salt, format_name
            )

            # 構建層元數據
            layer_metadata = {
                'num': i,
                'filename': os.path.basename(output_filename),
                'has_password': has_password,
                'is_master': is_master,
                'length': pwd_length if has_password and not is_master else None
            }

            # 加密存儲密碼
            if password and not is_master:
                encrypted_pwd = encrypt_password(password)
                if encrypted_pwd:
                    layer_metadata['encrypted_password'] = encrypted_pwd
                    logging.info(f"✅ 第 {i} 層密碼已加密存儲")
                else:
                    logging.warning(f"⚠️ 第 {i} 層密碼加密失敗，將使用元數據方式")

            password_metadata['layers'].append(layer_metadata)

            # 更新進度日誌
            progress_text = f"正在壓縮第 {i}/{iterations} 層 (格式: {format_name})"
            update_task_log(task_id, f"--- {progress_text} ---", is_progress_text=True)

            # 執行壓縮
            _process_compression_layer(current_file, output_filename, format_name, password)

            # 管理文件
            generated_files.append(output_filename)
            if current_file != original_file:
                os.remove(current_file)
            current_file = output_filename

            # 更新進度
            update_task_progress(task_id, int((i / iterations) * 100))

        update_task_log(task_id, "✅ 壓縮流程結束。", is_progress_text=True)

        # 完成壓縮任務（上傳、更新狀態、發送郵件）
        _finalize_compression(
            task_id, current_file, original_size, password_metadata,
            recipient_email, host_url, task_id_str, params['raw_filename']
        )
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
                _cleanup_files(generated_files)

def decompression_worker(task_id_str):
    task_id = ObjectId(task_id_str)
    task = tasks_collection.find_one({'_id': task_id});
    if not task: return
    params = task['params']; original_file = params['original_file']
    output_path = os.path.join(OUTPUT_FOLDER, f"{task_id_str}_decompress_temp")
    # 追蹤已處理的中間檔案
    processed_files = []
    try:
        password_list = params['password_list']
        master_pass = params.get('master_pass')
        if not password_list: raise ValueError("找不到可用的密碼表。")
        current_file = original_file; total_layers = len(password_list)
        total_uncompressed_size = 0
        for i, layer_info in enumerate(reversed(password_list)):
            # 在每層開始前檢查取消狀態
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
                # py7zr 支持多線程解壓縮
                with py7zr.SevenZipFile(current_file, 'r', password=password, mp=True) as z:
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
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; img-src 'self' data:; font-src 'self' https://cdn.jsdelivr.net; connect-src 'self'"
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
def index():
    logging.info(f"首頁訪問 - IP: {request.headers.get('X-Forwarded-For', request.remote_addr)}")
    return render_template('index.html')

@app.route('/compress', methods=['POST'])
def compress_route():
    if active_task_count >= MAX_CONCURRENT_TASKS:
        return jsonify({'error': '伺服器目前忙碌中，請稍後再試。'}), 429
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500

        # 調試：記錄請求信息
        logging.info(f"=== 接收到壓縮請求 ===")
        logging.info(f"Content-Type: {request.content_type}")
        logging.info(f"Content-Length: {request.headers.get('Content-Length', 'N/A')}")
        logging.info(f"IP: {request.headers.get('X-Forwarded-For', request.remote_addr)}")

        # 調試：檢查 request.files 和 request.form
        logging.info(f"request.files keys: {list(request.files.keys())}")
        logging.info(f"request.form keys: {list(request.form.keys())}")

        # 嘗試直接讀取 stream
        if hasattr(request, 'stream'):
            logging.info(f"request.stream 存在: {request.stream is not None}")

        file = request.files.get('file')
        logging.info(f"接收到檔案: {file.filename if file else 'None'}")

        if not file:
            # 檢查是否為被取消的上傳（有 Content-Length 但無 files/form 數據）
            content_length = request.headers.get('Content-Length')
            if content_length and not request.files.keys() and not request.form.keys():
                # 這是被取消的上傳，靜默處理（不記錄錯誤堆疊）
                logging.info(f"⚠️ 檢測到被取消的上傳請求 (Content-Length: {content_length}，但無數據)")
                return jsonify({'error': '上傳已取消'}), 400

            # 其他情況下記錄詳細信息
            if request.files:
                logging.info(f"request.files 內容: {[(k, v.filename) for k, v in request.files.items()]}")
            if request.form:
                logging.info(f"request.form 內容: {dict(request.form)}")
            logging.error("❌ 無法獲取檔案，可能是反向代理限制或 multipart 解析失敗")

        validate_file(file, mode='compress')

        # 取得來源 IP 位址
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)

        # 解析並驗證參數
        try:
            iterations = int(request.form.get('iterations', 5))
            master_pass_interval = int(request.form.get('master_password_interval', '10'))

            # 解析加密模式參數
            encrypt_mode = request.form.get('encrypt_mode', 'none')
            manual_layers = []
            multiple_interval = 3
            arithmetic_start = 1
            arithmetic_diff = 2

            if encrypt_mode == 'manual':
                manual_layers = [int(x.strip()) for x in request.form.get('manual_layers', '').split(',') if x.strip()]
            elif encrypt_mode == 'multiple':
                multiple_interval = int(request.form.get('multiple_interval', '3'))
            elif encrypt_mode == 'arithmetic':
                arithmetic_start = int(request.form.get('arithmetic_start', '1'))
                arithmetic_diff = int(request.form.get('arithmetic_diff', '2'))

            # 解析自訂密碼長度配置
            use_custom_length = request.form.get('use_custom_length') == 'on'
            password_length_config = {}
            default_password_length = 16

            if use_custom_length:
                default_password_length = int(request.form.get('default_password_length', '16'))
                config_str = request.form.get('password_length_config', '').strip()
                if config_str:
                    # 解析格式：1:32, 5:24, 10:32
                    for item in config_str.split(','):
                        item = item.strip()
                        if ':' in item:
                            layer, length = item.split(':', 1)
                            layer = int(layer.strip())
                            length = int(length.strip())
                            if 8 <= length <= 64:  # 限制密碼長度範圍
                                password_length_config[layer] = length

        except ValueError:
            raise ValueError("參數格式錯誤，請檢查數字欄位。")

        params = {
            'raw_filename': file.filename,
            'expected_filename': file.filename,
            'iterations': iterations,
            'encrypt_mode': encrypt_mode,
            'manual_layers': manual_layers,
            'multiple_interval': multiple_interval,
            'arithmetic_start': arithmetic_start,
            'arithmetic_diff': arithmetic_diff,
            'formats': [x.strip() for x in request.form.get('formats', 'zip,7z,targz').split(',') if x.strip()],
            'use_master_pass': request.form.get('use_master_pass') == 'on',
            'master_pass': request.form.get('master_password'),
            'master_pass_interval': master_pass_interval,
            'use_custom_length': use_custom_length,
            'password_length_config': password_length_config,
            'default_password_length': default_password_length
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

        # 調試：記錄解壓縮請求
        logging.info(f"=== 接收到解壓縮請求 ===")
        logging.info(f"IP: {request.headers.get('X-Forwarded-For', request.remote_addr)}")

        file = request.files.get('file')
        logging.info(f"接收到檔案: {file.filename if file else 'None'}")

        if not file:
            # 檢查是否為被取消的上傳（有 Content-Length 但無 files/form 數據）
            content_length = request.headers.get('Content-Length')
            if content_length and not request.files.keys() and not request.form.keys():
                # 這是被取消的上傳，靜默處理（不記錄錯誤堆疊）
                logging.info(f"⚠️ 檢測到被取消的上傳請求 (Content-Length: {content_length}，但無數據)")
                return jsonify({'error': '上傳已取消'}), 400

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
    # 驗證 ObjectId 格式
    object_id, error = validate_object_id(compress_task_id)
    if error:
        return jsonify({'error': error}), 400

    if active_task_count >= MAX_CONCURRENT_TASKS:
        return jsonify({'error': '伺服器目前忙碌中，請稍後再試。'}), 429
    try:
        if db is None: return jsonify({'error': '資料庫未連線'}), 500
        original_task = tasks_collection.find_one({'_id': object_id})
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

        # 優先從 Authorization header 讀取（更安全）
        # 格式：Authorization: Bearer <secret>
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            provided_secret = auth_header[7:]  # 移除 'Bearer ' 前綴
        else:
            # 向後兼容：仍支援 query parameter（但不建議使用）
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
    # 驗證 ObjectId 格式
    object_id, error = validate_object_id(task_id)
    if error:
        return jsonify({'error': error}), 400

    try:
        tasks_collection.update_one({'_id': object_id}, {'$set': {'cancel_requested': True}})
        return jsonify({'status': 'cancellation requested'})
    except Exception as e:
        return handle_route_exception(e, 'cancel')

@app.route('/status/<task_id>')
def task_status(task_id):
    # 驗證 ObjectId 格式
    object_id, error = validate_object_id(task_id)
    if error:
        return jsonify({'error': error}), 400

    try:
        # 1. 嘗試從快取獲取
        task = get_cached_task(task_id)

        # 2. 快取未命中，從資料庫讀取
        if not task:
            task = tasks_collection.find_one({'_id': object_id})
            if task:
                task['_id'] = str(task['_id'])

                # 向下兼容：如果有新格式元數據但沒有舊格式內容，則重新生成
                if task.get('password_metadata') and not task.get('password_file_content'):
                    master_pass = task.get('params', {}).get('master_pass')
                    task['password_file_content'] = regenerate_passwords_from_metadata(
                        task['password_metadata'],
                        master_pass
                    )

                # 存入快取（僅快取進行中和完成的任務）
                if task.get('status') in ['處理中', '完成']:
                    cache_task(task_id, task)
            else:
                return jsonify({'error': '找不到任務'}), 404
        else:
            # 從快取獲取時也需要處理密碼
            if task.get('password_metadata') and not task.get('password_file_content'):
                master_pass = task.get('params', {}).get('master_pass') if task.get('params') else None
                task['password_file_content'] = regenerate_passwords_from_metadata(
                    task['password_metadata'],
                    master_pass
                )

        return jsonify(task)
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

@app.route('/download-password/<task_id>')
def download_password_file(task_id):
    """
    下載密碼文件（支持新舊格式）
    - 舊格式：直接使用 password_file_content
    - 新格式：從 password_metadata 重新生成
    """
    # 驗證 ObjectId 格式
    object_id, error = validate_object_id(task_id)
    if error:
        return error, 400

    try:
        task = tasks_collection.find_one({'_id': object_id})
        if not task:
            return "任務不存在。", 404

        password_content = None

        # 優先使用舊格式（向下兼容）
        if task.get('password_file_content'):
            password_content = task['password_file_content']
        # 如果沒有舊格式，從新格式元數據重新生成
        elif task.get('password_metadata'):
            master_pass = task.get('params', {}).get('master_pass')
            password_content = regenerate_passwords_from_metadata(
                task['password_metadata'],
                master_pass
            )

        if not password_content:
            return "此任務沒有密碼信息。", 404

        # 創建文本文件響應
        password_bytes = io.BytesIO(password_content.encode('utf-8'))
        response = send_file(
            password_bytes,
            mimetype='text/plain',
            as_attachment=True,
            download_name='passwords.txt'
        )
        response.headers['Content-Disposition'] = "attachment; filename*=UTF-8''passwords.txt"
        return response
    except Exception as e:
        return handle_route_exception(e, 'download-password')

@app.route('/download/<task_id>')
def download_file(task_id):
    # 驗證 ObjectId 格式
    object_id, error = validate_object_id(task_id)
    if error:
        return error, 400

    try:
        task = tasks_collection.find_one({'_id': object_id})
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

