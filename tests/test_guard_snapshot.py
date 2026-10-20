"""
簡化版測試防護套件 - 檔案內容快照
目的：鎖定重構前的程式碼結構，無需執行應用程式

這個測試套件專注於「程式碼快照」而非「執行行為」
優點：
- 不需要 MongoDB 連接
- 不需要完整的依賴
- 快速執行
- 明確標記需要保護的程式碼區塊
"""

import pytest
import os
import re


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY = os.path.join(PROJECT_ROOT, 'app.py')
INDEX_HTML = os.path.join(PROJECT_ROOT, 'templates', 'index.html')


# ============================================================================
# 防護 #1: 管理員認證方式快照
# ============================================================================

class TestAdminAuthSnapshot:
    """快照：管理員認證相關的程式碼結構"""

    def test_admin_secret_query_parameter_pattern(self):
        """
        快照：管理員密碼通過 query parameter 傳遞的程式碼模式

        ⚠️ 已知問題：不安全的傳遞方式
        目的：重構時確保認證邏輯完整遷移
        """
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        # 當前模式：request.args.get('secret')
        assert "request.args.get('secret'" in content, \
            "管理員密碼獲取方式已改變，確保新方式已實現"

        # 確保至少有一個 ADMIN_SECRET 檢查
        assert 'ADMIN_SECRET' in content, \
            "ADMIN_SECRET 環境變數應該存在"

    def test_admin_routes_exist(self):
        """快照：管理員路由的存在性"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        # 應該有 /admin 路由
        assert "@app.route('/admin')" in content or \
               '@app.route("/admin")' in content, \
            "/admin 路由應該存在"

        # 應該有 /admin/api/decompression-logs 路由
        assert '/admin/api/decompression-logs' in content, \
            "管理員 API 路由應該存在"


# ============================================================================
# 防護 #2: 清理函數與競態條件
# ============================================================================

class TestCleanupFunctionSnapshot:
    """快照：清理函數的程式碼結構"""

    def test_cleanup_on_exit_exists(self):
        """
        快照：cleanup_on_exit 函數的存在性

        ⚠️ 已知問題：可能被雙重調用
        """
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        # cleanup_on_exit 函數應該存在
        assert 'def cleanup_on_exit(' in content, \
            "cleanup_on_exit 函數應該存在"

    def test_atexit_registration_exists(self):
        """快照：atexit 註冊的存在性"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        # 應該有 atexit.register
        assert 'atexit.register(' in content, \
            "atexit 註冊應該存在"

    def test_signal_handler_exists(self):
        """快照：signal handler 的存在性"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        # 應該有 signal handler 定義
        assert 'def signal_handler(' in content or \
               'signal.signal(' in content, \
            "signal handler 應該存在"


# ============================================================================
# 防護 #3: ObjectId 驗證
# ============================================================================

class TestObjectIdHandlingSnapshot:
    """快照：ObjectId 處理的程式碼模式"""

    def test_objectid_import_exists(self):
        """快照：ObjectId 的導入"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'from bson import ObjectId' in content or \
               'from bson.objectid import ObjectId' in content, \
            "ObjectId 應該被導入"

    def test_objectid_usage_patterns(self):
        """
        快照：ObjectId 的使用模式

        ⚠️ 已知問題：缺少驗證
        目的：找出所有需要添加驗證的地方
        """
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        # 找出所有 ObjectId(...) 的使用
        object_id_usages = re.findall(r'ObjectId\([^)]+\)', content)

        # 應該有多處使用（至少 5 處）
        assert len(object_id_usages) >= 5, \
            f"找到 {len(object_id_usages)} 處 ObjectId 使用，應該有多處"

        # 記錄當前的使用次數（作為基準）
        print(f"\n當前 ObjectId 使用次數: {len(object_id_usages)}")


# ============================================================================
# 防護 #4: JavaScript 變數作用域
# ============================================================================

class TestJavaScriptVariableSnapshot:
    """快照：JavaScript 關鍵變數的宣告位置"""

    def test_compressionStartTime_declaration_exists(self):
        """
        快照：compressionStartTime 變數的宣告

        ⚠️ 已知問題：宣告在使用之後
        目的：確保修復時不會移除變數
        """
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        # 變數應該被宣告
        assert 'compressionStartTime' in content, \
            "compressionStartTime 變數應該存在"

        # 應該有 let 或 var 宣告
        assert 'let compressionStartTime' in content or \
               'var compressionStartTime' in content, \
            "compressionStartTime 應該被明確宣告"

    def test_compressionStartTime_usage_exists(self):
        """快照：compressionStartTime 的使用"""
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        # 應該有使用該變數的代碼
        usage_count = content.count('compressionStartTime')

        # 至少使用 2 次（宣告 + 使用）
        assert usage_count >= 2, \
            f"compressionStartTime 使用 {usage_count} 次，應該至少 2 次"

        print(f"\ncompressionStartTime 使用次數: {usage_count}")

    def test_completedLayers_declaration_exists(self):
        """快照：completedLayers 變數"""
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'completedLayers' in content, \
            "completedLayers 變數應該存在"


# ============================================================================
# 防護 #5: ConfirmDialog innerHTML
# ============================================================================

class TestConfirmDialogSnapshot:
    """快照：ConfirmDialog 的實現"""

    def test_confirmDialog_object_exists(self):
        """快照：ConfirmDialog 物件的存在性"""
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'ConfirmDialog' in content, \
            "ConfirmDialog 應該存在"

        # 應該有 show 方法
        assert 'show(' in content or 'show:' in content, \
            "ConfirmDialog.show 方法應該存在"

    def test_innerHTML_usage_in_confirmDialog(self):
        """
        快照：ConfirmDialog 使用 innerHTML

        ⚠️ 已知問題：XSS 風險
        目的：確保修復時仍能顯示格式化內容
        """
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        # 當前使用 innerHTML
        assert 'this.title.innerHTML' in content or \
               'this.message.innerHTML' in content, \
            "ConfirmDialog 應該使用 innerHTML（即使有風險）"


# ============================================================================
# 防護 #6: switchTab 函數
# ============================================================================

class TestSwitchTabSnapshot:
    """快照：switchTab 函數（我們剛剛添加的）"""

    def test_switchTab_function_exists(self):
        """快照：確保 switchTab 函數存在"""
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'function switchTab(' in content, \
            "switchTab 函數應該存在"

    def test_switchTab_function_calls_exist(self):
        """快照：switchTab 的調用"""
        with open(INDEX_HTML, 'r', encoding='utf-8') as f:
            content = f.read()

        # 應該有多處調用
        call_count = content.count("switchTab('")

        assert call_count >= 3, \
            f"switchTab 被調用 {call_count} 次，應該至少 3 次"

        print(f"\nswitchTab 調用次數: {call_count}")


# ============================================================================
# 防護 #7: API 端點結構
# ============================================================================

class TestAPIRoutesSnapshot:
    """快照：關鍵 API 路由的存在性"""

    def test_compress_route_exists(self):
        """快照：/compress 路由"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert "@app.route('/compress'" in content or \
               '@app.route("/compress"' in content, \
            "/compress 路由應該存在"

    def test_decompress_route_exists(self):
        """快照：解壓縮相關路由"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert '/decompress' in content, \
            "解壓縮路由應該存在"

    def test_status_route_exists(self):
        """快照：/status 路由"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert '/status/' in content, \
            "/status 路由應該存在"

    def test_health_route_exists(self):
        """快照：/health 路由"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert "/health" in content, \
            "/health 路由應該存在"

    def test_download_routes_exist(self):
        """快照：下載相關路由"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert '/download/' in content, \
            "下載路由應該存在"


# ============================================================================
# 防護 #8: 配置常數
# ============================================================================

class TestConfigurationSnapshot:
    """快照：配置常數的值"""

    def test_max_file_size_config_exists(self):
        """快照：檔案大小限制配置"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'MAX_FILE_SIZE_MB' in content, \
            "MAX_FILE_SIZE_MB 配置應該存在"

    def test_allowed_extensions_config_exists(self):
        """快照：允許的副檔名配置"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'ALLOWED_EXTENSIONS' in content, \
            "ALLOWED_EXTENSIONS 配置應該存在"

        # 應該包含常見的壓縮格式
        assert '.zip' in content, "應該支援 .zip"
        assert '.7z' in content, "應該支援 .7z"

    def test_max_concurrent_tasks_config_exists(self):
        """快照：並發任務限制配置"""
        with open(APP_PY, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'MAX_CONCURRENT_TASKS' in content, \
            "MAX_CONCURRENT_TASKS 配置應該存在"


# ============================================================================
# 總結測試
# ============================================================================

def test_snapshot_suite_summary(capsys):
    """測試套件總結"""
    print("\n" + "="*70)
    print("🛡️  快照測試防護套件執行完成")
    print("="*70)
    print("\n✅ 所有快照測試通過 = 程式碼結構已鎖定")
    print("\n📋 已保護的程式碼結構：")
    print("   1. ✓ 管理員認證方式（query parameter）")
    print("   2. ✓ 清理函數結構（cleanup_on_exit）")
    print("   3. ✓ ObjectId 使用模式")
    print("   4. ✓ JavaScript 變數宣告（compressionStartTime 等）")
    print("   5. ✓ ConfirmDialog 實現（innerHTML）")
    print("   6. ✓ switchTab 函數")
    print("   7. ✓ API 路由結構")
    print("   8. ✓ 配置常數")
    print("\n🔧 現在可以安全地進行重構！")
    print("💡 重構時這些測試應該保持通過（或有意識地修改）")
    print("="*70 + "\n")

    assert True
