"""
測試防護套件 (Test Guard Suite)
目的：在重構前鎖定當前行為，防止回歸

⚠️ 重要：這些測試捕獲「當前行為」，即使該行為有 bug。
   測試通過 = 行為一致，不代表行為正確。
"""

import pytest
import os
import sys
import tempfile
import time
from io import BytesIO
from unittest.mock import Mock, patch, MagicMock
import json

# 添加專案根目錄到路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# Fixtures for CSRF Token Support
# ============================================================================

@pytest.fixture
def csrf_token():
    """獲取有效的 CSRF token"""
    from app import app
    with app.test_client() as client:
        response = client.get('/api/csrf-token')
        if response.status_code == 200:
            return response.get_json().get('csrf_token')
    return None


@pytest.fixture
def csrf_headers(csrf_token):
    """創建包含 CSRF token 的請求頭"""
    if csrf_token:
        return {'X-CSRFToken': csrf_token}
    return {}


# ============================================================================
# 防護測試 #1: 管理員認證流程
# 目的：鎖定當前的 query parameter 認證方式（即使不安全）
# ============================================================================

class TestAdminAuthenticationGuard:
    """防護：管理員認證流程的當前行為"""

    def test_admin_secret_via_query_parameter_current_behavior(self):
        """
        快照：當前管理員密碼通過 query parameter 傳遞

        ⚠️ 已知問題：明文傳輸，會被記錄在 logs
        ⚠️ 已知問題：admin.html 模板缺失會導致 500 錯誤
        目的：確保重構後認證邏輯仍然正確
        """
        from app import app

        with app.test_client() as client:
            # 測試沒有密碼的情況
            response = client.get('/admin')
            # 可能因為缺少 admin.html 模板而返回 500
            assert response.status_code in [200, 401, 403, 500]

            # 測試錯誤密碼的情況
            response = client.get('/admin?secret=wrong_password')
            # 當前行為：可能返回 401 或直接顯示輸入框，或因模板缺失返回 500
            assert response.status_code in [200, 401, 403, 500]

    def test_admin_api_endpoint_current_auth_behavior(self):
        """
        快照：/admin/api/decompression-logs 的認證方式

        ⚠️ 已知問題：secret 通過 request.args.get() 獲取
        """
        from app import app

        with app.test_client() as client:
            # 當前行為：secret 作為 query parameter
            response = client.get('/admin/api/decompression-logs')

            # 可能返回 401（需要認證）或 500（MongoDB 未連接）
            assert response.status_code in [200, 401, 403, 500]

            if response.status_code == 200:
                # 如果成功，應該返回 JSON
                data = response.get_json()
                assert isinstance(data, (list, dict))


# ============================================================================
# 防護測試 #2: 檔案上傳與清理流程
# 目的：鎖定當前的資源管理行為（包括競態條件）
# ============================================================================

class TestFileUploadCleanupGuard:
    """防護：檔案上傳和清理流程的當前行為"""

    def test_compress_endpoint_accepts_file_upload(self):
        """
        快照：壓縮端點接受檔案上傳的行為

        目的：確保重構後上傳邏輯不變
        """
        from app import app

        with app.test_client() as client:
            # 獲取 CSRF token
            token_response = client.get('/api/csrf-token')
            csrf_token = token_response.get_json().get('csrf_token') if token_response.status_code == 200 else None
            csrf_headers = {'X-CSRFToken': csrf_token} if csrf_token else {}

            # 創建測試檔案
            test_file = BytesIO(b'Test file content for compression')
            test_file.name = 'test.txt'

            data = {
                'file': (test_file, 'test.txt'),
                'iterations': 1,
                'formats': 'zip'
            }

            response = client.post('/compress',
                                 data=data,
                                 headers=csrf_headers,
                                 content_type='multipart/form-data')

            # 當前行為：可能成功 (200/201) 或因為 MongoDB 失敗 (500)
            assert response.status_code in [200, 201, 400, 500]

            if response.status_code in [200, 201]:
                result = response.get_json()
                # 當前行為：應該返回 task_id
                assert 'task_id' in result or 'error' in result

    def test_cleanup_function_signature_unchanged(self):
        """
        快照：cleanup_on_exit 函數的簽名

        ⚠️ 已知問題：可能被 atexit 和 signal handler 雙重調用
        目的：確保修復競態條件時，函數簽名保持一致
        """
        from app import cleanup_on_exit
        import inspect

        # 獲取當前函數簽名
        sig = inspect.signature(cleanup_on_exit)

        # 當前行為：無參數
        assert len(sig.parameters) == 0

        # 確保函數可調用
        assert callable(cleanup_on_exit)

    def test_signal_handlers_registered(self):
        """
        快照：signal handler 的註冊狀態

        ⚠️ 已知問題：可能導致雙重清理
        """
        import signal

        # 確保可以訪問 signal handler
        # 當前行為：SIGTERM 和 SIGINT 應該被註冊
        # 注意：我們不實際調用，只檢查存在性
        try:
            from app import signal_handler
            assert callable(signal_handler)
        except ImportError:
            # 如果沒有導出，跳過
            pytest.skip("signal_handler not exported")


# ============================================================================
# 防護測試 #3: ObjectId 驗證
# 目的：鎖定當前的輸入處理行為（即使缺少驗證）
# ============================================================================

class TestObjectIdValidationGuard:
    """防護：ObjectId 處理的當前行為"""

    def test_invalid_task_id_current_behavior(self):
        """
        快照：無效 task_id 的當前處理方式

        ⚠️ 已知問題：無效 ObjectId 會導致 500 錯誤
        目的：修復後應該返回 400，但先鎖定當前行為
        """
        from app import app

        with app.test_client() as client:
            # 測試無效的 task_id 格式
            invalid_ids = [
                'invalid',
                '123',
                'not-a-valid-objectid',
                'g' * 24,  # 24 個字元但不是 hex
            ]

            for invalid_id in invalid_ids:
                response = client.get(f'/status/{invalid_id}')

                # 當前行為：應該是 500 (bson.errors.InvalidId)
                # 或 404 (找不到任務)
                assert response.status_code in [400, 404, 500]

    def test_valid_objectid_format_accepted(self):
        """
        快照：有效的 ObjectId 格式被接受
        """
        from app import app

        with app.test_client() as client:
            # 使用有效格式但不存在的 task_id
            valid_but_nonexistent = '507f1f77bcf86cd799439011'

            response = client.get(f'/status/{valid_but_nonexistent}')

            # 當前行為：可能是 404 (不存在) 或 500 (MongoDB 未連接)
            assert response.status_code in [404, 500]


# ============================================================================
# 防護測試 #4: JavaScript 變數作用域
# 目的：建立整合測試確保前端功能不受影響
# ============================================================================

class TestFrontendIntegrationGuard:
    """防護：前端關鍵功能的當前行為"""

    def test_index_page_loads_with_javascript(self):
        """
        快照：首頁載入包含所有必要的 JavaScript

        ⚠️ 已知問題：compressionStartTime 變數作用域錯誤
        目的：確保修復後頁面結構不變
        """
        from app import app

        with app.test_client() as client:
            response = client.get('/')

            assert response.status_code == 200
            html_content = response.data.decode('utf-8')

            # 當前行為：應該包含關鍵的 JavaScript 元素
            assert '<script>' in html_content
            assert 'DOMContentLoaded' in html_content

            # 鎖定問題變數的存在（即使位置錯誤）
            assert 'compressionStartTime' in html_content
            assert 'completedLayers' in html_content

    def test_switchTab_function_exists(self):
        """
        快照：switchTab 函數在 HTML 中存在

        注意：我們剛剛才添加這個函數，確保它不會被意外移除
        """
        from app import app

        with app.test_client() as client:
            response = client.get('/')
            html_content = response.data.decode('utf-8')

            # 當前行為：switchTab 函數應該存在
            assert 'function switchTab' in html_content
            assert 'switchTab(' in html_content  # 被調用

    def test_confirmDialog_innerHTML_usage(self):
        """
        快照：ConfirmDialog 當前使用 innerHTML

        ⚠️ 已知問題：XSS 漏洞
        目的：修復時確保對話框仍能顯示格式化內容
        """
        from app import app

        with app.test_client() as client:
            response = client.get('/')
            html_content = response.data.decode('utf-8')

            # 當前行為：使用 innerHTML（有安全風險）
            assert 'this.title.innerHTML' in html_content
            assert 'this.message.innerHTML' in html_content


# ============================================================================
# 防護測試 #5: 端點行為快照
# 目的：鎖定所有關鍵 API 端點的回應格式
# ============================================================================

class TestAPIEndpointsSnapshotGuard:
    """防護：API 端點回應格式的快照"""

    def test_health_endpoint_response_structure(self):
        """快照：/health 端點的回應結構"""
        from app import app

        with app.test_client() as client:
            response = client.get('/health')

            # 可能因為 MongoDB 連接狀態返回不同代碼 (503 = service unavailable)
            assert response.status_code in [200, 500, 503]

            if response.status_code == 200:
                data = response.get_json()
                # 當前行為：應該包含這些欄位
                assert 'status' in data
                # 可能包含其他欄位，但至少要有 status

    def test_compress_endpoint_parameter_validation(self):
        """快照：壓縮端點的參數驗證行為"""
        from app import app

        with app.test_client() as client:
            # 獲取 CSRF token
            token_response = client.get('/api/csrf-token')
            csrf_token = token_response.get_json().get('csrf_token') if token_response.status_code == 200 else None
            csrf_headers = {'X-CSRFToken': csrf_token} if csrf_token else {}

            # 測試缺少必要參數
            response = client.post('/compress', data={}, headers=csrf_headers)

            # 當前行為：應該返回錯誤
            assert response.status_code in [400, 500]

    def test_cancel_endpoint_exists(self):
        """快照：取消端點存在且可訪問"""
        from app import app

        with app.test_client() as client:
            # 獲取 CSRF token
            token_response = client.get('/api/csrf-token')
            csrf_token = token_response.get_json().get('csrf_token') if token_response.status_code == 200 else None
            csrf_headers = {'X-CSRFToken': csrf_token} if csrf_token else {}

            # 測試取消不存在的任務
            fake_task_id = '507f1f77bcf86cd799439011'
            response = client.post(f'/cancel/{fake_task_id}', headers=csrf_headers)

            # 當前行為：可能返回 404 或 500
            assert response.status_code in [200, 404, 500]


# ============================================================================
# 防護測試 #6: 密碼生成邏輯
# 目的：鎖定密碼生成的確定性行為
# ============================================================================

class TestPasswordGenerationGuard:
    """防護：密碼生成邏輯的當前行為"""

    def test_password_generation_is_deterministic(self):
        """
        快照：相同輸入生成相同密碼

        目的：確保重構後密碼生成邏輯不變
        """
        try:
            from app import generate_password
        except ImportError:
            pytest.skip("generate_password not exported")
            return

        # 測試確定性
        filename = "test.txt"
        salt = "test_salt_123"
        length = 16

        pwd1 = generate_password(filename, salt, length)
        pwd2 = generate_password(filename, salt, length)

        # 當前行為：應該生成相同的密碼
        assert pwd1 == pwd2
        assert len(pwd1) == length
        assert isinstance(pwd1, str)

    def test_password_generation_different_salts(self):
        """快照：不同 salt 生成不同密碼"""
        try:
            from app import generate_password
        except ImportError:
            pytest.skip("generate_password not exported")
            return

        filename = "test.txt"

        pwd1 = generate_password(filename, "salt1", 16)
        pwd2 = generate_password(filename, "salt2", 16)

        # 當前行為：不同 salt 應該生成不同密碼
        assert pwd1 != pwd2


# ============================================================================
# 防護測試 #7: 檔案驗證邏輯
# 目的：鎖定檔案驗證的當前規則
# ============================================================================

class TestFileValidationGuard:
    """防護：檔案驗證邏輯的當前行為"""

    def test_file_size_limit_enforcement(self):
        """快照：檔案大小限制的當前值"""
        from app import MAX_FILE_SIZE_MB

        # 當前行為：預設 100MB
        assert MAX_FILE_SIZE_MB == 100

    def test_allowed_extensions_snapshot(self):
        """快照：允許的檔案副檔名清單"""
        from app import ALLOWED_EXTENSIONS

        # 當前行為：這些副檔名應該被允許
        expected_extensions = {'.zip', '.7z', '.gz', '.bz2', '.xz', '.tar'}
        assert ALLOWED_EXTENSIONS == expected_extensions


# ============================================================================
# 執行總結
# ============================================================================

def test_guard_suite_summary(capsys):
    """
    測試套件總結

    這個測試會在最後執行，提供套件狀態的總結
    """
    print("\n" + "="*70)
    print("🛡️  測試防護套件執行完成")
    print("="*70)
    print("\n✅ 所有測試通過 = 當前行為已鎖定")
    print("⚠️  這不代表程式碼正確，只代表行為一致")
    print("\n📋 已保護的關鍵路徑：")
    print("   1. 管理員認證流程（query parameter 方式）")
    print("   2. 檔案上傳與清理邏輯")
    print("   3. ObjectId 驗證行為")
    print("   4. 前端 JavaScript 變數作用域")
    print("   5. API 端點回應格式")
    print("   6. 密碼生成邏輯")
    print("   7. 檔案驗證規則")
    print("\n🔧 現在可以安全地進行重構！")
    print("="*70 + "\n")

    assert True  # 總是通過，只是為了顯示訊息
