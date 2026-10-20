#!/usr/bin/env python3
"""
密碼加密密鑰生成工具

用途：生成 Fernet 加密密鑰用於加密壓縮任務的密碼

使用方法：
    python generate_encryption_key.py

輸出：
    將生成一個 Fernet 密鑰並顯示在終端
    將此密鑰設置為環境變數 PASSWORD_ENCRYPTION_KEY
"""

from cryptography.fernet import Fernet

def generate_key():
    """生成新的 Fernet 加密密鑰"""
    key = Fernet.generate_key()
    return key.decode('utf-8')

if __name__ == '__main__':
    print("=" * 70)
    print("密碼加密密鑰生成工具")
    print("=" * 70)
    print()

    key = generate_key()

    print("✅ 已生成新的加密密鑰：")
    print()
    print(f"    {key}")
    print()
    print("=" * 70)
    print("如何使用此密鑰：")
    print("=" * 70)
    print()
    print("1. 將此密鑰添加到 .env 文件：")
    print(f"   PASSWORD_ENCRYPTION_KEY={key}")
    print()
    print("2. 或在 Zeabur/Docker 中設置環境變數：")
    print(f"   PASSWORD_ENCRYPTION_KEY={key}")
    print()
    print("=" * 70)
    print("⚠️  重要提示：")
    print("=" * 70)
    print()
    print("• 請妥善保管此密鑰，遺失後無法解密已存儲的密碼")
    print("• 不要將密鑰提交到版本控制系統（Git）")
    print("• 每個部署環境應使用獨立的密鑰")
    print("• 更換密鑰後，舊任務的密碼將無法解密")
    print()
    print("=" * 70)
