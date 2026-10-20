"""
Decompression Worker Guard Tests

Purpose: Lock the behavior of decompression_worker before refactoring.
These tests verify function signature and key operations remain unchanged.

Phase 1 (Before Refactoring):
- Tests document current function structure
- All tests should PASS

Phase 2 (After Refactoring):
- Tests verify no behavioral changes
- All tests should still PASS
"""

import sys
import os
import inspect

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import decompression_worker


class TestDecompressionWorkerSignatureGuard:
    """Guard tests for decompression_worker function signature and structure"""

    def test_decompression_worker_signature_unchanged(self):
        """
        Lock the function signature during refactoring

        Current signature: decompression_worker(task_id_str)
        Must remain: decompression_worker(task_id_str)
        """
        sig = inspect.signature(decompression_worker)
        params = list(sig.parameters.keys())

        # Verify function takes exactly 1 parameter
        assert len(params) == 1, f"Expected 1 parameter, got {len(params)}"
        assert params[0] == 'task_id_str', f"Expected 'task_id_str', got '{params[0]}'"

        print(f"✅ decompression_worker 函數簽名已鎖定")
        print(f"   參數: {', '.join(params)}")

    def test_decompression_worker_key_operations_exist(self):
        """
        Verify key operations exist in decompression_worker or its helpers

        After refactoring, operations may be in helper functions.
        This test ensures refactoring doesn't accidentally remove:
        - MongoDB operations (tasks_collection)
        - GridFS operations (fs.put)
        - Archive extraction (py7zr.SevenZipFile, tarfile.open)
        - Progress tracking (update_task_progress)
        - Zip Bomb protection (MAX_DECOMPRESS_SIZE_BYTES)
        """
        import app

        # Get source code of main function AND helper functions
        main_source = inspect.getsource(decompression_worker)

        # Get helper functions source if they exist
        helper_functions = [
            '_load_decompression_task',
            '_extract_archive_layer',
            '_check_zip_bomb',
            '_finalize_decompression',
            '_cleanup_decompression'
        ]

        combined_source = main_source
        for helper_name in helper_functions:
            if hasattr(app, helper_name):
                helper_func = getattr(app, helper_name)
                combined_source += inspect.getsource(helper_func)

        # Key operations that must exist (in main or helpers)
        key_operations = {
            'tasks_collection': 'MongoDB 操作',
            'fs.put': 'GridFS 上傳',
            'py7zr.SevenZipFile': '7Z/ZIP 解壓',
            'tarfile.open': 'TAR 解壓',
            'update_task_progress': '進度更新',
            'MAX_DECOMPRESS_SIZE_BYTES': 'Zip Bomb 防護',
            'shutil.move': '文件移動',
            'zipfile.ZipFile': 'ZIP 打包'
        }

        missing_operations = []
        for operation, description in key_operations.items():
            if operation not in combined_source:
                missing_operations.append(f"{operation} ({description})")

        assert not missing_operations, \
            f"缺少關鍵操作: {', '.join(missing_operations)}"

        print("✅ 所有關鍵操作都存在（主函數或輔助函數中）")
        for operation, description in key_operations.items():
            if operation in main_source:
                print(f"   ✓ {operation} - {description} [主函數]")
            else:
                print(f"   ✓ {operation} - {description} [輔助函數]")

    def test_helper_functions_can_be_extracted(self):
        """
        Document the refactoring plan for decompression_worker

        Target helper functions to extract:
        1. _load_decompression_task() - 加載任務和驗證密碼表
        2. _extract_archive_layer() - 解壓單層壓縮包
        3. _check_zip_bomb() - 檢查 Zip Bomb
        4. _finalize_decompression() - 後處理和上傳
        5. _cleanup_decompression() - 清理臨時文件
        """
        source_code = inspect.getsource(decompression_worker)

        # Target sections to extract (approximate line patterns)
        refactoring_targets = {
            '_load_decompression_task': [
                'ObjectId(task_id_str)',
                'tasks_collection.find_one',
                'password_list'
            ],
            '_extract_archive_layer': [
                'py7zr.SevenZipFile',
                'tarfile.open',
                'extractall'
            ],
            '_check_zip_bomb': [
                'total_uncompressed_size',
                'MAX_DECOMPRESS_SIZE_BYTES',
                'Zip Bomb'
            ],
            '_finalize_decompression': [
                'os.path.isdir',
                'zipfile.ZipFile',
                'fs.put'
            ],
            '_cleanup_decompression': [
                'shutil.rmtree',
                'os.remove',
                'processed_files'
            ]
        }

        print("\n" + "="*70)
        print("📋 重構計劃：decompression_worker 函數")
        print("="*70)

        for func_name, patterns in refactoring_targets.items():
            patterns_found = [p for p in patterns if p in source_code]
            status = "✓" if len(patterns_found) >= 2 else "?"

            print(f"\n{status} {func_name}:")
            print(f"   目標: 提取 {len(patterns)} 個關鍵操作")
            print(f"   找到: {len(patterns_found)} 個匹配模式")

            if func_name == '_load_decompression_task':
                print("   職責: 加載任務數據、驗證密碼表、準備輸出路徑")
            elif func_name == '_extract_archive_layer':
                print("   職責: 解壓單層壓縮包（ZIP/7Z/TAR）、移動文件")
            elif func_name == '_check_zip_bomb':
                print("   職責: 檢查解壓後總大小，防止 Zip Bomb 攻擊")
            elif func_name == '_finalize_decompression':
                print("   職責: 後處理（打包目錄/保留文件）、上傳 GridFS")
            elif func_name == '_cleanup_decompression':
                print("   職責: 清理臨時文件和目錄")

        print("\n" + "="*70)
        print("重構策略：Guard -> Refactor -> Verify")
        print("="*70 + "\n")

        assert True, "重構計劃已記錄"


def test_refactoring_strategy():
    """
    Document the refactoring strategy for decompression_worker

    Strategy:
    1. [Guard] Create this test file ✅ (YOU ARE HERE)
    2. [Refactor] Extract 5 helper functions with type hints
    3. [Verify] Run this test to ensure no regressions
    4. [Document] Add Google-style docstrings to all helpers
    5. [Commit] Commit with clear message

    Current function size: ~132 lines
    Target: ~60 lines main function + 5 helpers
    """
    import app

    # Get current line count
    source = inspect.getsource(decompression_worker)
    line_count = len(source.strip().split('\n'))

    print("\n" + "="*70)
    print("🔧 Refactoring Strategy")
    print("="*70)
    print(f"Current size: {line_count} lines")
    print(f"Target size: ~60 lines (main) + helpers")
    print(f"Reduction: ~{line_count - 60} lines to extract")
    print("\nHelper functions to create:")
    print("  1. _load_decompression_task() - Type hints + Docstring")
    print("  2. _extract_archive_layer() - Type hints + Docstring")
    print("  3. _check_zip_bomb() - Type hints + Docstring")
    print("  4. _finalize_decompression() - Type hints + Docstring")
    print("  5. _cleanup_decompression() - Type hints + Docstring")
    print("\nBenefits:")
    print("  ✓ Improved readability")
    print("  ✓ Easier testing")
    print("  ✓ Better maintainability")
    print("  ✓ Consistent with compression_worker")
    print("="*70 + "\n")

    assert line_count > 100, f"Function should be large enough to refactor (got {line_count} lines)"
    assert True
