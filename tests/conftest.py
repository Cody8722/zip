"""
Pytest 配置文件
為測試防護套件提供共享的 fixtures 和配置
"""

import pytest
import os
import sys

# 添加專案根目錄到 Python 路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """
    設置測試環境

    這個 fixture 會在所有測試開始前執行一次
    """
    # 設置測試模式環境變數
    os.environ['TESTING'] = 'true'

    # 如果沒有設置 MONGO_URI，使用假的（避免測試失敗）
    if 'MONGO_URI' not in os.environ:
        os.environ['MONGO_URI'] = 'mongodb://localhost:27017/test_db'

    print("\n" + "="*70)
    print("🛡️  測試防護套件啟動")
    print("="*70)
    print("目的：鎖定重構前的當前行為")
    print("注意：測試通過 ≠ 程式碼正確，只代表行為一致")
    print("="*70 + "\n")

    yield

    # 清理
    if 'TESTING' in os.environ:
        del os.environ['TESTING']


@pytest.fixture(scope="function")
def mock_mongodb():
    """
    Mock MongoDB 連接

    用於不需要真實資料庫連接的測試
    """
    from unittest.mock import Mock, MagicMock

    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_collection = MagicMock()
    mock_fs = MagicMock()

    # 設置基本的 mock 行為
    mock_client.__getitem__ = lambda self, key: mock_db
    mock_db.__getitem__ = lambda self, key: mock_collection

    return {
        'client': mock_client,
        'db': mock_db,
        'collection': mock_collection,
        'fs': mock_fs
    }


@pytest.fixture(scope="function")
def test_file():
    """
    創建測試用的臨時檔案
    """
    from io import BytesIO

    content = b'This is a test file content for guard suite testing.'
    file_obj = BytesIO(content)
    file_obj.name = 'test_guard_file.txt'

    return file_obj


@pytest.fixture(scope="function")
def snapshot_current_config():
    """
    捕獲當前的配置值

    這個 fixture 會記錄測試開始時的配置狀態
    """
    try:
        from app import (
            MAX_FILE_SIZE_MB,
            ALLOWED_EXTENSIONS,
            MAX_CONCURRENT_TASKS,
        )

        return {
            'max_file_size': MAX_FILE_SIZE_MB,
            'allowed_extensions': ALLOWED_EXTENSIONS.copy(),
            'max_concurrent_tasks': MAX_CONCURRENT_TASKS,
        }
    except ImportError:
        return {}


def pytest_configure(config):
    """
    Pytest 配置鉤子
    """
    # 添加自定義標記
    config.addinivalue_line(
        "markers", "guard: 標記為防護測試（鎖定當前行為）"
    )
    config.addinivalue_line(
        "markers", "critical: 標記為關鍵路徑測試"
    )
    config.addinivalue_line(
        "markers", "security: 標記為安全相關測試"
    )


def pytest_collection_modifyitems(config, items):
    """
    修改測試收集行為
    """
    for item in items:
        # 為 test_guard_suite.py 中的所有測試添加 guard 標記
        if 'test_guard_suite' in str(item.fspath):
            item.add_marker(pytest.mark.guard)
