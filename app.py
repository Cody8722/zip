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
    """產生一個指定長度的隨機密碼"""
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

