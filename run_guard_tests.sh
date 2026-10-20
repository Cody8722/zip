#!/bin/bash

# 測試防護套件執行腳本
# 目的：在重構前執行所有防護測試，確保當前行為被鎖定

echo "=================================="
echo "🛡️  執行測試防護套件"
echo "=================================="
echo ""
echo "目的：鎖定當前行為，防止重構時的回歸"
echo "注意：測試通過 ≠ 程式碼正確"
echo ""
echo "=================================="
echo ""

# 設置 Python 路徑
export PYTHONPATH="${PYTHONPATH}:$(pwd)"

# 檢查 pytest 是否安裝
if ! command -v pytest &> /dev/null; then
    echo "❌ pytest 未安裝，正在安裝..."
    pip install pytest pytest-cov
fi

# 執行防護測試
echo "🔍 執行防護測試..."
echo ""

pytest tests/test_guard_suite.py \
    -v \
    --tb=short \
    --color=yes \
    -m guard \
    --durations=10

# 獲取測試結果
TEST_RESULT=$?

echo ""
echo "=================================="

if [ $TEST_RESULT -eq 0 ]; then
    echo "✅ 所有防護測試通過！"
    echo ""
    echo "📋 已鎖定的行為："
    echo "   ✓ 管理員認證流程"
    echo "   ✓ 檔案上傳與清理"
    echo "   ✓ ObjectId 驗證"
    echo "   ✓ 前端 JavaScript 功能"
    echo "   ✓ API 端點回應格式"
    echo "   ✓ 密碼生成邏輯"
    echo "   ✓ 檔案驗證規則"
    echo ""
    echo "🔧 現在可以安全地進行重構！"
    echo ""
    echo "下一步："
    echo "1. 開始修復高優先級問題"
    echo "2. 每次修改後重新執行此腳本"
    echo "3. 確保所有測試保持綠色"
else
    echo "❌ 部分測試失敗"
    echo ""
    echo "⚠️  這可能表示："
    echo "   - 環境配置問題（如 MongoDB 未連接）"
    echo "   - 某些功能已經損壞"
    echo ""
    echo "建議："
    echo "1. 檢查上方的錯誤訊息"
    echo "2. 修復環境問題"
    echo "3. 或更新測試以反映實際行為"
fi

echo "=================================="
echo ""

exit $TEST_RESULT
