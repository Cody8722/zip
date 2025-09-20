# 步驟 1: 選擇一個輕量級的 Python 官方版本作為基礎
FROM python:3.11-slim

# 步驟 2: 在容器中建立一個工作資料夾
WORKDIR /app

# 步驟 3: 複製我們的「零件說明書」，並讓機器人先去把所有零件準備好
# 這是最花時間的一步，但因為我們把它獨立出來，Zeabur 就能將結果快取起來！
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 步驟 4: 將我們所有其他的程式碼，複製進這個已經準備好零件的環境中
COPY . .

# 步驟 5: 告訴容器，當它啟動時，應該執行什麼指令來開啟我們的網站
# 這取代了舊的 Procfile 的功能
CMD ["gunicorn", "app:app", "--timeout", "120"]

