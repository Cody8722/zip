# 步驟 1: 選擇一個輕量級的 Python 官方版本作為基礎
FROM python:3.11-slim

# 步驟 2: 建立一個權限受限的獨立使用者，提升安全性
RUN groupadd -r appuser && useradd -r -g appuser appuser

# 步驟 3: 在容器中建立一個工作資料夾
WORKDIR /app

# 步驟 4: 複製我們的「零件說明書」，並讓機器人先去把所有零件準備好
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 步驟 5: 將我們所有其他的程式碼，複製進這個已經準備好零件的環境中
COPY . .

# 步驟 6: 建立程式需要的暫存資料夾，並將所有檔案的所有權，都交給我們新建立的工作人員
RUN mkdir -p /tmp/compressor_uploads /tmp/compressor_outputs && \
    chown -R appuser:appuser /app /tmp/compressor_uploads /tmp/compressor_outputs

# 步驟 7: 切換到我們新建立的、權限較低的工作人員身分
USER appuser

# 步驟 8: 告訴容器，當它啟動時，應該執行什麼指令來開啟我們的網站
CMD ["gunicorn", "app:app", "--timeout", "120"]

