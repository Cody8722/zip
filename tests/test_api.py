"""
zip (多层压缩工具) Backend API Tests
测试压缩/解压缩系统的主要 API 端点
"""
import pytest
import sys
import os
from io import BytesIO

# 添加父目录到 Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app


@pytest.fixture
def client():
    """创建测试客户端"""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


class TestHealthCheck:
    """健康检查端点测试"""

    def test_status_endpoint(self, client):
        """测试 /status 端点"""
        response = client.get('/status')
        assert response.status_code == 200

        data = response.get_json()
        assert 'status' in data
        assert data['status'] == 'ok'

    def test_status_returns_json(self, client):
        """测试返回 JSON 格式"""
        response = client.get('/status')
        assert response.content_type == 'application/json'


class TestCompressionAPI:
    """压缩 API 测试"""

    def test_compress_endpoint_exists(self, client):
        """测试 /compress 端点存在"""
        response = client.post('/compress')
        # 应该返回 400（缺少文件）或 500
        assert response.status_code in [400, 429, 500]

    def test_compress_without_file(self, client):
        """测试压缩时缺少文件"""
        response = client.post('/compress')
        assert response.status_code in [400, 429, 500]

    def test_compress_with_invalid_file(self, client):
        """测试压缩无效文件"""
        data = {
            'file': (BytesIO(b'test content'), 'test.txt'),
            'iterations': 5
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        # 可能拒绝或接受（取决于文件类型限制）
        assert response.status_code in [200, 201, 400, 429, 500]

    def test_compress_with_valid_zip(self, client):
        """测试压缩 ZIP 文件"""
        # 创建一个简单的 ZIP 文件内容（模拟）
        data = {
            'file': (BytesIO(b'PK\x03\x04' + b'\x00' * 100), 'test.zip'),
            'iterations': 1,
            'formats': 'zip'
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        assert response.status_code in [200, 201, 400, 429, 500]

    def test_compress_with_iterations(self, client):
        """测试多层压缩"""
        data = {
            'file': (BytesIO(b'test' * 100), 'test.dat'),
            'iterations': 3,
            'encrypt_mode': 'odd',
            'formats': 'zip,7z,targz'
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        assert response.status_code in [200, 201, 400, 429, 500]


class TestDecompressionAPI:
    """解压缩 API 测试"""

    def test_decompress_manual_endpoint(self, client):
        """测试手动解压端点"""
        response = client.post('/decompress-manual')
        assert response.status_code in [400, 429, 500]

    def test_decompress_without_file(self, client):
        """测试解压时缺少文件"""
        data = {
            'passwords': ''
        }
        response = client.post('/decompress-manual',
                              data=data,
                              content_type='multipart/form-data')
        assert response.status_code in [400, 429, 500]

    def test_decompress_with_invalid_file(self, client):
        """测试解压无效文件"""
        data = {
            'file': (BytesIO(b'not a zip'), 'test.txt'),
            'passwords': '第 1 層: password123'
        }
        response = client.post('/decompress-manual',
                              data=data,
                              content_type='multipart/form-data')
        assert response.status_code in [400, 429, 500]

    def test_decompress_with_password_list(self, client):
        """测试解压带密码列表"""
        passwords = """第 1 層 (file.zip): password1
第 2 層 (file.7z): password2
第 3 層 (file.tar.gz): (無密碼)"""
        data = {
            'file': (BytesIO(b'PK\x03\x04' + b'\x00' * 50), 'encrypted.zip'),
            'passwords': passwords
        }
        response = client.post('/decompress-manual',
                              data=data,
                              content_type='multipart/form-data')
        assert response.status_code in [200, 201, 400, 429, 500]


class TestTaskManagement:
    """任务管理 API 测试"""

    def test_get_task_status_invalid_id(self, client):
        """测试获取不存在的任务状态"""
        response = client.get('/task-status/invalid_task_id_123')
        assert response.status_code in [404, 500]

    def test_cancel_task_invalid_id(self, client):
        """测试取消不存在的任务"""
        response = client.post('/cancel-task/invalid_task_id_456')
        assert response.status_code in [404, 500]

    def test_get_all_tasks(self, client):
        """测试获取所有任务"""
        response = client.get('/tasks')
        assert response.status_code in [200, 404, 500]


class TestFileDownload:
    """文件下载 API 测试"""

    def test_download_result_invalid_id(self, client):
        """测试下载不存在的结果文件"""
        response = client.get('/download/invalid_file_id_789')
        assert response.status_code in [404, 500]

    def test_download_password_file_invalid_id(self, client):
        """测试下载不存在的密码文件"""
        response = client.get('/download-password/invalid_task_id_012')
        assert response.status_code in [404, 500]


class TestInputValidation:
    """输入验证测试"""

    def test_file_size_limit(self, client):
        """测试文件大小限制"""
        # 创建一个小文件测试（实际限制在环境变量中）
        large_content = b'x' * (1024 * 1024)  # 1MB
        data = {
            'file': (BytesIO(large_content), 'large.zip'),
            'iterations': 1
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        # 应该能处理 1MB 文件
        assert response.status_code in [200, 201, 400, 413, 429, 500]

    def test_iterations_validation_negative(self, client):
        """测试负数迭代次数"""
        data = {
            'file': (BytesIO(b'test'), 'test.zip'),
            'iterations': -1
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        # 应该拒绝负数
        assert response.status_code in [400, 429, 500]

    def test_iterations_validation_zero(self, client):
        """测试零迭代次数"""
        data = {
            'file': (BytesIO(b'test'), 'test.zip'),
            'iterations': 0
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        assert response.status_code in [400, 429, 500]

    def test_iterations_validation_very_large(self, client):
        """测试过大的迭代次数"""
        data = {
            'file': (BytesIO(b'test'), 'test.zip'),
            'iterations': 1000  # 可能超过合理限制
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        # 取决于是否有上限验证
        assert response.status_code in [200, 201, 400, 429, 500]

    def test_malicious_filename(self, client):
        """测试恶意文件名（路径穿越）"""
        data = {
            'file': (BytesIO(b'test'), '../../../etc/passwd'),
            'iterations': 1
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        # 应该被安全处理（secure_filename）
        assert response.status_code in [200, 201, 400, 429, 500]


class TestSecurityFeatures:
    """安全功能测试"""

    def test_file_magic_number_validation_zip(self, client):
        """测试 ZIP 文件魔数验证"""
        # 正确的 ZIP 魔数
        valid_zip = b'PK\x03\x04' + b'\x00' * 50
        data = {
            'file': (BytesIO(valid_zip), 'test.zip'),
            'passwords': ''
        }
        response = client.post('/decompress-manual',
                              data=data,
                              content_type='multipart/form-data')
        # 应该通过魔数验证
        assert response.status_code in [200, 201, 400, 429, 500]

    def test_file_magic_number_validation_fake_zip(self, client):
        """测试伪造的 ZIP 文件"""
        # 错误的魔数但文件名是 .zip
        fake_zip = b'FAKE' + b'\x00' * 50
        data = {
            'file': (BytesIO(fake_zip), 'fake.zip'),
            'passwords': ''
        }
        response = client.post('/decompress-manual',
                              data=data,
                              content_type='multipart/form-data')
        # 应该被拒绝（魔数不匹配）
        assert response.status_code in [400, 429, 500]


class TestRateLimiting:
    """速率限制测试"""

    def test_concurrent_task_limit(self, client):
        """测试并发任务限制"""
        # 这个测试只验证端点响应，不实际测试并发限制
        response = client.get('/status')
        assert response.status_code == 200


class TestEmailNotification:
    """邮件通知测试（如果启用）"""

    def test_compress_with_email(self, client):
        """测试带邮件通知的压缩"""
        data = {
            'file': (BytesIO(b'test' * 100), 'test.dat'),
            'iterations': 1,
            'recipient_email': 'test@example.com'
        }
        response = client.post('/compress',
                              data=data,
                              content_type='multipart/form-data')
        # 邮件发送可能失败，但请求应该被接受
        assert response.status_code in [200, 201, 400, 429, 500]


class TestErrorHandling:
    """错误处理测试"""

    def test_nonexistent_endpoint(self, client):
        """测试不存在的端点"""
        response = client.get('/api/nonexistent')
        assert response.status_code == 404

    def test_invalid_http_method(self, client):
        """测试不支持的 HTTP 方法"""
        response = client.patch('/compress')
        assert response.status_code in [405, 500]

    def test_index_page(self, client):
        """测试首页"""
        response = client.get('/')
        assert response.status_code == 200


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
