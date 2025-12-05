# CLAUDE.md - AI Assistant Guide for Zip Compression Tool

## Project Overview

**Repository:** Multi-layer File Compression Tool
**Type:** Flask-based Web Application
**Primary Function:** Multi-layer file compression/decompression with encryption support
**Deployment:** Zeabur platform via Docker
**Tech Stack:** Python 3.11, Flask, MongoDB, GridFS, py7zr, Tailwind CSS

### Core Capabilities
- Multi-layer compression with configurable formats (ZIP, 7Z, TAR.GZ/BZ2/XZ)
- Password encryption support with random or master password options
- Asynchronous task processing with progress tracking
- Email notifications for task completion
- Admin dashboard for task monitoring
- Security features: file validation, magic number checking, Zip Bomb protection

---

## Repository Structure

```
/home/user/zip/
├── .github/
│   ├── copilot-instructions.md    # GitHub Copilot AI guide (Chinese)
│   └── workflows/
│       └── ci-cd.yml              # GitHub Actions CI/CD pipeline
├── templates/
│   └── index.html                 # Frontend SPA (66KB, Tailwind CSS)
├── tests/
│   ├── __init__.py
│   └── test_api.py                # Comprehensive API test suite (313 lines)
├── app.py                         # Main Flask application (630 lines)
├── requirements.txt               # Python dependencies
├── Dockerfile                     # Multi-stage Docker build
├── .env.example                   # Environment variable template
└── README.md                      # User documentation (Chinese)
```

### Key Files Analysis

**app.py** (630 lines)
- Lines 1-66: Imports, configuration, MongoDB initialization
- Lines 67-114: Utility functions (password generation, validation)
- Lines 115-274: Background workers (compression/decompression)
- Lines 275-630: Flask routes and API endpoints

**test_api.py** (313 lines)
- 10 test classes covering all major functionality
- Tests include: health checks, compression, decompression, validation, security, error handling
- Uses pytest fixtures for test client setup

**templates/index.html** (66KB)
- Single-page application with dual tabs (encoder/decoder)
- Real-time progress updates via polling (/status/<task_id> every 2 seconds)
- Tailwind CSS for styling, vanilla JavaScript for interactivity

---

## Architecture & Design Patterns

### System Architecture

```
┌─────────────┐          ┌──────────────┐          ┌─────────────┐
│   Browser   │◄────────►│  Flask App   │◄────────►│  MongoDB    │
│  (Frontend) │  HTTP    │   (app.py)   │   CRUD   │  (tasks DB) │
└─────────────┘          └──────────────┘          └─────────────┘
                                │                          │
                                │                   ┌──────▼──────┐
                         ┌──────▼──────┐           │   GridFS    │
                         │ ThreadPool  │           │ (file store)│
                         │  Executor   │           └─────────────┘
                         └─────────────┘
                                │
                      ┌─────────┴─────────┐
                      │                   │
              ┌───────▼────────┐  ┌──────▼────────┐
              │ compression_   │  │ decompression_│
              │   worker()     │  │   worker()    │
              └────────────────┘  └───────────────┘
```

### Key Design Patterns

1. **Background Task Processing**
   - ThreadPoolExecutor with configurable max workers (default: 3)
   - Task wrapper pattern for resource tracking
   - Global lock for thread-safe task counting

2. **Database Abstraction**
   - MongoDB for task metadata and logs
   - GridFS for large file storage (completed files)
   - TTL indexes for automatic cleanup (1 hour expiry)

3. **File Processing Pipeline**
   ```
   Upload → Validate → Process (Layers) → Store → Cleanup
   ```

4. **Error Handling Strategy**
   - Exception-specific catching for file format errors
   - Generic fallback with logging
   - Client-facing error messages in Chinese
   - Detailed server logs in English

---

## Development Workflows

### Setup & Installation

```bash
# 1. Clone repository
git clone <repo-url>
cd zip

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with actual MONGO_URI

# 4. Run tests
pip install pytest pytest-cov flake8
pytest tests/ -v

# 5. Run locally
python app.py
# or
gunicorn app:app --timeout 120
```

### Environment Variables

**Required:**
- `MONGO_URI`: MongoDB connection string (format: mongodb+srv://user:pass@cluster.mongodb.net/)

**Optional:**
- `ADMIN_SECRET`: Admin dashboard password
- `MAIL_USERNAME`: SMTP email username (for notifications)
- `MAIL_PASSWORD`: SMTP email password
- `MAX_CONCURRENT_TASKS`: Thread pool size (default: 3)
- `MAX_FILE_SIZE_MB`: Upload limit (default: 100)

### Testing Strategy

**Test Execution:**
```bash
# Run all tests with coverage
pytest --cov=. --cov-report=term-missing

# Run specific test class
pytest tests/test_api.py::TestCompressionAPI -v

# Run with flake8 linting
flake8 . --count --max-line-length=127 --statistics
```

**Test Classes:**
- `TestHealthCheck`: Index page, status endpoints
- `TestCompressionAPI`: Multi-layer compression scenarios
- `TestDecompressionAPI`: Password-based decompression
- `TestTaskManagement`: Task lifecycle operations
- `TestFileDownload`: Result and password file downloads
- `TestInputValidation`: Size limits, iteration bounds
- `TestSecurityFeatures`: Magic number validation, malicious files
- `TestRateLimiting`: Concurrent task limits
- `TestEmailNotification`: Email notification flow
- `TestErrorHandling`: 404/405 responses

**Important Testing Notes:**
- Tests use `app.url_map.strict_slashes = False` to prevent 301 redirects
- Accepts status codes: 200, 201, 400, 429, 500 (due to MongoDB dependency)
- File validation includes magic number checks for ZIP/7Z formats

### CI/CD Pipeline (.github/workflows/ci-cd.yml)

**Triggers:**
- Push to `main`/`master` branches
- Pull requests to `main`/`master`
- Manual dispatch (`workflow_dispatch`)
- Weekly schedule (Sundays at 00:00)

**Jobs:**
1. **lint-and-test** (Python 3.11)
   - flake8 linting (syntax errors, complexity checks)
   - pytest with coverage reporting
   - Template file validation

2. **docker-build** (depends on lint-and-test)
   - Docker Buildx setup
   - Image build verification

3. **security-scan** (depends on lint-and-test)
   - `safety` tool for dependency vulnerabilities
   - Non-blocking warnings

**Deployment:**
- Zeabur auto-deploys from Git on successful push to main/master
- No manual deployment step in workflow

---

## API Endpoints Reference

### Public Routes

| Endpoint | Method | Purpose | Request Body | Response |
|----------|--------|---------|--------------|----------|
| `/` | GET | Serve frontend SPA | - | HTML page |
| `/compress` | POST | Start compression task | multipart/form-data (file, iterations, formats, encrypt_mode, recipient_email) | `{"task_id": "..."}` |
| `/decompress-manual` | POST | Start manual decompression | multipart/form-data (file, passwords, master_pass) | `{"task_id": "..."}` |
| `/start-shared-decompression/<compress_task_id>` | POST | Decompress using original task passwords | `{"master_pass": "..."}` (if used) | `{"decompress_task_id": "..."}` |
| `/status/<task_id>` | GET | Poll task progress | - | `{"status": "...", "progress": 0-100, "logs": [...], "progress_text": "..."}` |
| `/cancel/<task_id>` | POST | Cancel running task | - | `{"message": "..."}` |
| `/download/<file_id>` | GET | Download result file (GridFS) | - | File stream |
| `/download-password/<task_id>` | GET | Download password text file | - | Text file |
| `/health` | GET | Health check (MongoDB ping) | - | `{"status": "healthy/unhealthy", "mongodb": "...", "active_tasks": N}` |
| `/storage-stats` | GET | GridFS storage statistics | - | `{"total_files": N, "total_size_mb": X, ...}` |

### Admin Routes (Require ADMIN_SECRET)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/admin` | GET | Admin dashboard HTML |
| `/admin/api/decompression-logs` | GET | Retrieve decompression task logs |

### Request/Response Examples

**Compression Request:**
```bash
curl -X POST http://localhost:5000/compress \
  -F "file=@document.pdf" \
  -F "iterations=5" \
  -F "formats=zip,7z,targz" \
  -F "encrypt_mode=odd" \
  -F "recipient_email=user@example.com"
```

**Response:**
```json
{
  "task_id": "507f1f77bcf86cd799439011",
  "message": "壓縮任務已啟動"
}
```

**Status Polling:**
```bash
curl http://localhost:5000/status/507f1f77bcf86cd799439011
```

```json
{
  "status": "處理中",
  "progress": 60,
  "progress_text": "正在壓縮第 3/5 層 (格式: targz)",
  "logs": [
    "--- 正在壓縮第 1/5 層 (格式: zip) ---",
    "--- 正在壓縮第 2/5 層 (格式: 7z) ---",
    "--- 正在壓縮第 3/5 層 (格式: targz) ---"
  ]
}
```

---

## Key Components Deep Dive

### 1. File Validation System (app.py:91-114)

**Security Checks:**
- Extension whitelist: `.zip`, `.7z`, `.gz`, `.bz2`, `.xz`, `.tar`
- File size limit: Configurable via `MAX_FILE_SIZE_MB` (default 100MB)
- Magic number validation:
  - ZIP: `PK\x03\x04`, `PK\x05\x06`, `PK\x07\x08`
  - 7Z: `7z\xbc\xaf'\x1c`

**Implementation:**
```python
def validate_file(file, mode='compress'):
    # 1. Check file existence and filename
    # 2. Verify size limit by seeking to end
    # 3. For decompress mode:
    #    - Check extension against ALLOWED_EXTENSIONS
    #    - Read first 8 bytes for magic number
    #    - Validate magic number matches extension claim
    # 4. Raise ValueError with Chinese error messages
```

### 2. Task State Management

**MongoDB Document Schema:**
```python
{
  "_id": ObjectId("..."),
  "status": "處理中" | "完成" | "失敗",
  "progress": 0-100,              # Integer percentage
  "progress_text": "...",         # Current operation description
  "logs": ["...", "..."],         # Array of log messages
  "params": {                     # Original request parameters
    "original_file": "/tmp/...",
    "iterations": 5,
    "formats": ["zip", "7z"],
    "encrypt_odd": true,
    "manual_layers": [],
    "use_master_pass": false,
    "raw_filename": "original.pdf"
  },
  "created_at": ISODate("..."),
  "result_file_id": "...",        # GridFS file ID (on completion)
  "result_filename": "...",       # Download filename
  "password_file_content": "...", # Password list text
  "delete_token": "...",          # Security token for deletion
  "cancel_requested": false       # Cancellation flag
}
```

**State Transition Helpers:**
```python
update_task_log(task_id, message, is_progress_text=False)
# -> Appends to logs array, optionally sets progress_text

update_task_progress(task_id, progress)
# -> Sets progress field (0-100)
```

### 3. Compression Worker (app.py:126-181)

**Algorithm Flow:**
```python
for layer in range(1, iterations + 1):
    # 1. Check cancellation flag
    if task.cancel_requested:
        return

    # 2. Determine format (cycle through formats list)
    format = formats[(layer - 1) % len(formats)]

    # 3. Determine encryption
    if use_master_pass and layer % master_pass_interval == 0:
        password = master_pass
    elif (encrypt_odd and layer is odd) or (layer in manual_layers):
        password = generate_password(12)
    else:
        password = None

    # 4. Compress with appropriate library
    if format in ('zip', '7z'):
        py7zr.SevenZipFile(output, 'w', password=password)
    else:  # tar.gz/bz2/xz
        tarfile.open(output, 'w:gz')

    # 5. Clean up previous layer file
    os.remove(previous_layer_file)

    # 6. Update progress
    update_task_progress(task_id, (layer / iterations) * 100)

    # 7. Record password in password file content
    password_file_content += f"第 {layer} 層 (...): {password or '(無密碼)'}\n"
```

**Final Steps:**
- Upload result to GridFS
- Delete local file
- Generate delete token (secrets.token_hex(16))
- Update task status to '完成'
- Send email notification (if requested)

### 4. Decompression Worker (app.py:182-274)

**Security Features:**
- Zip Bomb protection: Max decompressed size 1GB (`MAX_DECOMPRESS_SIZE_BYTES`)
- Size tracking across all layers

**Algorithm Flow:**
```python
for layer in reversed(password_list):  # Decompress from outer to inner
    # 1. Check cancellation flag

    # 2. Handle master password placeholder
    if password == 'MASTER_PASSWORD_PLACEHOLDER':
        password = master_pass

    # 3. Extract to temp directory
    if file.endswith(('.zip', '.7z')):
        py7zr.SevenZipFile(..., password=password).extractall()
    else:
        tarfile.open(...).extractall()

    # 4. Check decompressed size (Zip Bomb protection)
    total_size += current_layer_size
    if total_size > 1GB:
        raise Exception("Zip Bomb detected")

    # 5. Move extracted content out of temp directory
    shutil.move(extracted_item, OUTPUT_FOLDER)

    # 6. Update progress
```

**Post-Processing:**
- If final result is directory: Package as ZIP with original filename
- If final result is single file: Keep as-is with original filename
- Upload to GridFS
- Update task status

### 5. Password Parsing (app.py:78-89)

**Input Format:**
```
第 1 層 (file.zip): password123
第 2 層 (file.7z): (無密碼)
第 3 層 (file.tar.gz): (特殊密碼層)
```

**Output:**
```python
[
  {"filename": "file.zip", "password": "password123"},
  {"filename": "file.7z", "password": None},
  {"filename": "file.tar.gz", "password": "MASTER_PASSWORD_PLACEHOLDER"}
]
```

**Regex Pattern:**
```python
r'第 \d+ 層 \((.*?)\):\s*(.*)'
```

### 6. Email Notification System (app.py:275-289)

**Trigger:** Compression task completion (if `recipient_email` provided)

**SMTP Configuration:**
- Server: smtp.gmail.com:587
- Requires `MAIL_USERNAME` and `MAIL_PASSWORD` environment variables
- Uses TLS encryption

**Email Content:**
- Subject: Multi-language task completion notice
- Body: Download link with task ID
- Format: Plain text

---

## Common Development Tasks

### 1. Adding a New Compression Format

**Example: Adding RAR support**

```python
# Step 1: Add to formats dictionary (app.py ~line 134)
formats = {
    'zip': '.zip',
    '7z': '.7z',
    'targz': '.tar.gz',
    'rar': '.rar'  # NEW
}

# Step 2: Install Python library
# pip install rarfile
# Add to requirements.txt: rarfile

# Step 3: Import library (app.py top)
import rarfile

# Step 4: Add compression logic in compression_worker()
if format_name == 'rar':
    with rarfile.RarFile(output_filename, 'w', password=password) as rf:
        rf.write(current_file, os.path.basename(current_file))

# Step 5: Add decompression logic in decompression_worker()
if layer_info['filename'].endswith('.rar'):
    with rarfile.RarFile(current_file, 'r', password=password) as rf:
        rf.extractall(path=output_path)

# Step 6: Update file validation (app.py:50)
ALLOWED_EXTENSIONS = {'.zip', '.7z', '.gz', '.bz2', '.xz', '.tar', '.rar'}

# Step 7: Add magic number validation (app.py ~line 112)
if filename_lower.endswith('.rar') and not header.startswith(b'Rar!\x1a\x07'):
    raise ValueError("檔案宣稱是 RAR 檔，但內容格式不符")

# Step 8: Update frontend (templates/index.html)
# - Add RAR to format selection dropdown
# - Update format descriptions

# Step 9: Write tests
def test_compress_with_rar(self, client):
    data = {
        'file': (BytesIO(b'Rar!\x1a\x07' + b'\x00' * 100), 'test.rar'),
        'iterations': 1,
        'formats': 'rar'
    }
    response = client.post('/compress', data=data, content_type='multipart/form-data')
    assert response.status_code in [200, 201]
```

### 2. Modifying Task Progress Tracking

**Current System:** Manual progress updates in workers

**To Add Substep Progress:**

```python
# Example: Show compression ratio during operation

# In compression_worker(), after compressing each layer:
original_size = os.path.getsize(current_file)
compressed_size = os.path.getsize(output_filename)
ratio = (1 - compressed_size/original_size) * 100

update_task_log(
    task_id,
    f"第 {i} 層壓縮完成 (壓縮率: {ratio:.1f}%)",
    is_progress_text=False  # Add to logs but don't update progress_text
)
```

### 3. Implementing Task Cleanup/Deletion

**Current State:** Files remain in GridFS indefinitely

**To Add Auto-Cleanup:**

```python
# Option A: Extend MongoDB TTL index to GridFS
# Create TTL index on tasks collection (add to app.py initialization)
tasks_collection.create_index("created_at", expireAfterSeconds=3600)

# Add cleanup job for GridFS files
def cleanup_orphaned_files():
    """Remove GridFS files older than 1 hour"""
    cutoff_time = datetime.now() - timedelta(hours=1)
    old_tasks = tasks_collection.find({
        "created_at": {"$lt": cutoff_time},
        "result_file_id": {"$exists": True}
    })

    for task in old_tasks:
        try:
            fs.delete(ObjectId(task['result_file_id']))
            logging.info(f"Deleted GridFS file {task['result_file_id']}")
        except Exception as e:
            logging.error(f"Failed to delete file: {e}")

# Run cleanup periodically (e.g., via APScheduler)
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_orphaned_files, 'interval', hours=1)
scheduler.start()
```

### 4. Adding Request Rate Limiting

**Current State:** Only concurrent task limit (MAX_CONCURRENT_TASKS)

**To Add IP-Based Rate Limiting:**

```python
# Install: pip install flask-limiter
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# Apply to compress endpoint
@app.route('/compress', methods=['POST'])
@limiter.limit("10 per hour")  # 10 compression tasks per hour per IP
def compress_route():
    # ... existing code
```

### 5. Debugging Failed Tasks

**Steps:**

```bash
# 1. Check application logs
docker logs <container_id> | grep "ERROR"

# 2. Query MongoDB for failed tasks
mongo <connection_string>
use compressor_db
db.tasks.find({"status": "失敗"}).sort({"created_at": -1}).limit(10)

# 3. Inspect task logs
db.tasks.findOne({"_id": ObjectId("...")}).logs

# 4. Check file system state
ls -lh /tmp/compressor_uploads
ls -lh /tmp/compressor_outputs

# 5. Verify GridFS files
db.fs.files.count()
db.fs.files.find().sort({"uploadDate": -1}).limit(5)
```

**Common Failure Causes:**
- Corrupted archive files → Check magic number validation logs
- Incorrect passwords → Review password parsing logic
- Disk space exhaustion → Monitor `/tmp` partition
- MongoDB connection timeout → Check `MONGO_URI` and network
- Zip Bomb triggers → Review decompression size logs

---

## Best Practices for AI Assistants

### Code Modification Guidelines

1. **Always Read Before Editing**
   - Use `Read` tool on files before making changes
   - Understand context and surrounding code
   - Check for similar patterns elsewhere in codebase

2. **Preserve Language Consistency**
   - User-facing messages: Chinese (Traditional)
   - Code comments: Chinese (existing style)
   - Logs: Mix of Chinese (user) and English (technical)
   - Variable names: English
   - Documentation: English (this file), Chinese (README.md)

3. **Security First**
   - Never disable file validation
   - Always use `secure_filename()` for user uploads
   - Validate input sizes and types
   - Check for path traversal attempts
   - Maintain magic number validation

4. **Testing Requirements**
   - Add tests for any new API endpoints
   - Test both success and failure cases
   - Include edge cases (empty files, huge iterations, etc.)
   - Verify status code ranges: 200/201 (success), 400 (bad request), 429 (rate limit), 500 (server error)

5. **Error Handling**
   - Catch specific exceptions before generic ones
   - Always update task status on failure
   - Log full stack traces with `exc_info=True`
   - Return user-friendly Chinese error messages
   - Never expose internal paths or credentials

6. **Performance Considerations**
   - Clean up temporary files immediately after use
   - Use streaming for large file transfers
   - Don't load entire files into memory
   - Consider Zip Bomb protection for all decompression
   - Monitor ThreadPoolExecutor task queue

### Git Workflow

**Branch Strategy:**
- Main branch: `main` (or `master`)
- Feature branches: `claude/claude-md-<session-id>`
- Always develop on assigned branch (from task description)

**Commit Message Format:**
```
<type>: <concise description>

<optional detailed explanation>

Examples:
- Fix test failures - update tests to match actual API routes
- Add comprehensive unit tests for zip compression tool API
- Fix flake8 F824 errors - Remove unnecessary global declarations
```

**Before Committing:**
```bash
# 1. Run linter
flake8 . --count --max-line-length=127

# 2. Run tests
pytest tests/ -v

# 3. Check diff
git diff

# 4. Stage changes
git add <files>

# 5. Commit with descriptive message
git commit -m "..."

# 6. Push to feature branch
git push -u origin <branch-name>
```

### Interaction Patterns

**When Asked to Add Features:**
1. Analyze existing code structure
2. Identify similar patterns in codebase
3. Propose implementation approach
4. Ask for clarification if requirements are ambiguous
5. Implement following established conventions
6. Add corresponding tests
7. Update documentation if necessary

**When Asked to Debug:**
1. Ask for error messages/logs
2. Check recent git commits for related changes
3. Review test failures
4. Inspect relevant code sections
5. Propose fix with explanation
6. Verify fix doesn't break other functionality

**When Asked to Explain:**
1. Reference specific line numbers (file:line format)
2. Provide code context
3. Explain in terms of project architecture
4. Link to related components
5. Offer examples if helpful

---

## Security Considerations

### Input Validation

**File Uploads:**
- ✅ Extension whitelist (ALLOWED_EXTENSIONS)
- ✅ Size limits (MAX_FILE_SIZE_MB)
- ✅ Magic number validation
- ✅ Filename sanitization (secure_filename)
- ❌ Antivirus scanning (not implemented)
- ❌ Content-Type validation (not implemented)

**User Input:**
- ✅ Iteration count validation (prevent resource exhaustion)
- ✅ Password format parsing with regex
- ❌ CSRF protection (not implemented for POST endpoints)
- ❌ Input sanitization for XSS in logs (minimal risk as logs are not rendered as HTML)

### Authentication & Authorization

**Current State:**
- No user authentication system
- Admin dashboard protected by `ADMIN_SECRET` query parameter
- Tasks are publicly accessible by ID (UUID-based, not guessable)
- Delete tokens for task deletion (secrets.token_hex)

**Recommendations for Production:**
- Implement proper user authentication (JWT, OAuth)
- Add CSRF tokens to all forms
- Implement role-based access control
- Add rate limiting per user/IP
- Secure admin routes with proper auth middleware

### File System Security

**Sandboxing:**
- ✅ Uses `/tmp/compressor_*` directories (ephemeral)
- ✅ Docker USER directive (non-root user)
- ✅ Temporary file cleanup after processing
- ❌ No chroot/jail isolation
- ❌ No disk quota enforcement

**Path Traversal Prevention:**
- ✅ `secure_filename()` for uploads
- ✅ `os.path.basename()` for extractions
- ❌ No explicit path validation for archive contents (py7zr/tarfile handle this)

### Denial of Service Protection

**Implemented:**
- ✅ File size limits (100MB default)
- ✅ Concurrent task limits (3 default)
- ✅ Zip Bomb protection (1GB decompressed limit)
- ✅ Task cancellation mechanism

**Missing:**
- ❌ Request rate limiting (per IP/user)
- ❌ Timeout for individual compression operations
- ❌ Memory usage limits per task
- ❌ Decompression depth limits (nested archives)

### Dependency Security

**Monitoring:**
- CI/CD includes `safety` scan for known vulnerabilities
- Non-blocking warnings (should be addressed)

**Current Dependencies:**
```
Flask - Web framework
py7zr - 7Z/ZIP handling (potential vulnerability surface)
gunicorn - Production server
pymongo - MongoDB driver
dnspython - DNS resolution for MongoDB
cryptography - Encryption primitives
qrcode[pil] - QR code generation
```

**Recommendations:**
- Regularly update dependencies: `pip list --outdated`
- Monitor security advisories for py7zr and Flask
- Consider dependency pinning for production

### Data Privacy

**Sensitive Data:**
- Task parameters (stored in MongoDB)
- Uploaded files (temporary, GridFS after completion)
- Generated passwords (stored in task documents and password files)
- Email addresses (if provided)

**Current Protections:**
- TTL indexes for automatic data expiration
- No logging of passwords in application logs
- HTTPS recommended for production (not enforced in code)

**Missing:**
- Encryption at rest for MongoDB
- PII data scrubbing in logs
- GDPR compliance features (data export, deletion requests)

---

## Troubleshooting Guide

### MongoDB Connection Issues

**Symptoms:**
- "❌ 應用程式啟動失敗" in logs
- Health check returns unhealthy status
- Tasks fail immediately

**Solutions:**
```bash
# 1. Verify MONGO_URI format
echo $MONGO_URI
# Should be: mongodb+srv://user:pass@cluster.mongodb.net/

# 2. Test connection manually
mongosh "$MONGO_URI"

# 3. Check network connectivity
ping cluster.mongodb.net

# 4. Verify MongoDB Atlas IP whitelist
# - Add current server IP to Atlas Network Access

# 5. Check MongoDB Atlas cluster status
# - Login to cloud.mongodb.com
# - Verify cluster is running
```

### File Processing Errors

**Symptoms:**
- Tasks stuck at "處理中"
- "❌ 檔案格式錯誤或已損毀" in logs
- Partial files in /tmp directories

**Solutions:**
```python
# 1. Enable debug logging
logging.basicConfig(level=logging.DEBUG)

# 2. Check file type detection
# Add logging in compression_worker():
logging.debug(f"Processing file: {current_file}, format: {format_name}")

# 3. Verify library compatibility
# Test py7zr version:
import py7zr
print(py7zr.__version__)

# 4. Manual file inspection
file <path_to_file>
hexdump -C <path_to_file> | head
```

### Frontend Not Updating

**Symptoms:**
- Progress stuck at 0%
- Logs not appearing
- Download button not showing

**Solutions:**
```javascript
// 1. Check browser console for errors
// Open DevTools > Console

// 2. Verify polling endpoint
// In browser console:
fetch('/status/YOUR_TASK_ID').then(r => r.json()).then(console.log)

// 3. Check CORS issues (if API and frontend on different domains)
// Add to app.py:
from flask_cors import CORS
CORS(app)

// 4. Verify task_id format
// Should be 24-character hex string (ObjectId)
```

### Docker Build Failures

**Symptoms:**
- "Error: failed to build" in CI/CD
- Permission denied in container

**Solutions:**
```dockerfile
# 1. Verify Python version compatibility
# Dockerfile uses python:3.11-slim

# 2. Check requirements.txt compatibility
# Test locally:
docker build -t zip-test .
docker run zip-test python -c "import py7zr; print('OK')"

# 3. Verify user permissions
# Ensure appuser owns all necessary directories
RUN chown -R appuser:appuser /app /tmp/compressor_uploads /tmp/compressor_outputs

# 4. Test gunicorn startup
docker run -e MONGO_URI=$MONGO_URI zip-test gunicorn app:app --timeout 120
```

### Test Failures

**Common Issues:**

```python
# 1. 301 Redirects (strict_slashes)
# Fixed in test_api.py line 21:
app.url_map.strict_slashes = False

# 2. MongoDB dependency in tests
# Tests accept 500 status for routes requiring DB
assert response.status_code in [200, 201, 400, 429, 500]

# 3. Flake8 F824 errors (global declarations)
# Remove unnecessary global statements:
# Bad:  global client
# Good: client = MongoClient(...)

# 4. Test isolation
# Ensure each test is independent
# Use fixtures for setup/teardown
```

---

## Deployment Checklist

### Pre-Deployment

- [ ] All tests passing (`pytest tests/ -v`)
- [ ] Flake8 linting clean (`flake8 . --count`)
- [ ] Environment variables documented in `.env.example`
- [ ] MongoDB database created and accessible
- [ ] GridFS collections initialized
- [ ] Admin secret generated (`openssl rand -hex 32`)
- [ ] Email credentials configured (if using notifications)
- [ ] HTTPS certificate configured (production)
- [ ] Backup strategy defined for MongoDB

### Zeabur Deployment

1. **Connect Repository:**
   - Link GitHub repository to Zeabur
   - Select `main` branch for auto-deploy

2. **Configure Environment Variables:**
   ```
   MONGO_URI=mongodb+srv://...
   ADMIN_SECRET=<generated_secret>
   MAIL_USERNAME=<gmail_address>
   MAIL_PASSWORD=<app_password>
   MAX_CONCURRENT_TASKS=3
   MAX_FILE_SIZE_MB=100
   ```

3. **Verify Deployment:**
   - Check `/health` endpoint
   - Test compression workflow
   - Monitor logs for errors

4. **Post-Deployment:**
   - Test email notifications
   - Verify file downloads
   - Check admin dashboard access
   - Monitor resource usage

### Monitoring

**Key Metrics:**
- `/health` - MongoDB connectivity, active tasks
- `/storage-stats` - GridFS usage, file count
- Docker logs - Application errors, warnings
- MongoDB Atlas metrics - Connection count, operations/sec

**Alerts to Set Up:**
- MongoDB connection failures
- Disk space < 10% free
- Task failure rate > 10%
- Response time > 5 seconds

---

## Useful Commands Reference

### Development

```bash
# Run app locally
python app.py

# Run with gunicorn (production-like)
gunicorn app:app --timeout 120 --workers 2

# Run tests
pytest tests/ -v --cov=. --cov-report=html

# Lint code
flake8 . --max-line-length=127 --statistics

# Check for security vulnerabilities
pip install safety
safety check

# Generate requirements from environment
pip freeze > requirements.txt
```

### Docker

```bash
# Build image
docker build -t zip-compressor .

# Run container locally
docker run -p 5000:5000 \
  -e MONGO_URI="mongodb+srv://..." \
  zip-compressor

# Debug container
docker run -it --entrypoint /bin/bash zip-compressor

# Check logs
docker logs <container_id> -f
```

### MongoDB Operations

```bash
# Connect to MongoDB
mongosh "$MONGO_URI"

# Switch to database
use compressor_db

# View tasks
db.tasks.find().sort({created_at: -1}).limit(10)

# Check GridFS files
db.fs.files.find().pretty()

# Clean up old tasks manually
db.tasks.deleteMany({created_at: {$lt: new Date(Date.now() - 3600000)}})

# Create TTL index
db.tasks.createIndex({created_at: 1}, {expireAfterSeconds: 3600})

# View collection stats
db.tasks.stats()
db.fs.files.stats()
```

### Git Operations

```bash
# Check current branch
git branch --show-current

# Create feature branch
git checkout -b claude/feature-name

# Stage changes
git add app.py tests/test_api.py

# Commit
git commit -m "Add new feature: X"

# Push with upstream tracking
git push -u origin claude/feature-name

# View recent commits
git log --oneline -10

# Check status
git status
```

---

## Additional Resources

### Project Documentation
- `README.md` - User guide (Chinese)
- `.github/copilot-instructions.md` - GitHub Copilot guide (Chinese)
- `.env.example` - Environment variable reference

### External Documentation
- [Flask Documentation](https://flask.palletsprojects.com/)
- [py7zr Documentation](https://py7zr.readthedocs.io/)
- [MongoDB Python Driver](https://pymongo.readthedocs.io/)
- [GridFS Specification](https://www.mongodb.com/docs/manual/core/gridfs/)
- [Zeabur Documentation](https://zeabur.com/docs)

### Related Standards
- [ZIP File Format](https://en.wikipedia.org/wiki/ZIP_(file_format))
- [7Z Format](https://www.7-zip.org/7z.html)
- [TAR Archive Format](https://www.gnu.org/software/tar/manual/html_node/Standard.html)

---

## Changelog

### 2025-01-19 (Current State)
- Comprehensive test suite (313 lines, 10 test classes)
- CI/CD pipeline with linting, testing, Docker build, security scan
- Fixed test failures (strict_slashes, flake8 F824 errors)
- Production-ready Dockerfile with security best practices
- GridFS integration for large file storage
- Email notification system
- Admin dashboard
- Zip Bomb protection
- Magic number validation for security

### Known Issues
- No CSRF protection on POST endpoints
- No user authentication system
- Admin auth uses query parameter (should use header/session)
- Email notification errors are non-blocking
- No request rate limiting per IP
- GridFS files not automatically cleaned up with TTL

### Planned Improvements
- Implement proper authentication/authorization
- Add CSRF tokens
- Implement IP-based rate limiting
- Add automatic GridFS cleanup job
- Enhance error reporting in frontend
- Add task history/analytics
- Support for more archive formats (RAR, BZIP2)

---

**Document Version:** 1.0
**Last Updated:** 2025-01-19
**Maintained By:** AI Assistant (Claude)
**Target Audience:** AI assistants working on this codebase
