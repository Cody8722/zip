# TODO - Post-Release Tasks

## Low Priority

- [ ] Fix missing admin.html template
  - **Issue:** Template file `admin.html` is referenced in app.py but doesn't exist
  - **Impact:** Admin dashboard route returns 500 error
  - **Test Affected:** `test_admin_secret_via_query_parameter_current_behavior` (flaky)
  - **Severity:** Low (admin functionality exists, just missing UI)
  - **Suggested Fix:** Create `templates/admin.html` with admin dashboard UI

## Medium Priority

- [ ] Migrate admin authentication to header-based
  - **Current:** Admin secret passed via query parameter (visible in logs/URLs)
  - **Target:** Use Authorization header or session-based auth
  - **Benefit:** Improved security (no secrets in URLs)
  - **Reference:** See RELEASE_NOTES.md "Admin Authentication Enhancement"

- [ ] Add rate limiting per IP/user
  - **Current:** Only concurrent task limit (MAX_CONCURRENT_TASKS=3)
  - **Target:** Implement flask-limiter for per-IP rate limiting
  - **Benefit:** Prevent abuse and DoS attacks
  - **Suggested Limits:** 10 compressions/hour per IP, 200 requests/day

## High Priority (Future Releases)

- [ ] Implement user authentication system
  - Replace task-based access with user accounts
  - Add session management (JWT or Flask-Login)
  - Enable task history per user

- [ ] Add antivirus scanning for uploads
  - Integrate ClamAV or similar
  - Scan files before compression/decompression
  - Reject malicious files with clear error messages

- [ ] Performance optimization
  - Profile worker functions for bottlenecks
  - Consider async/await for I/O operations
  - Optimize GridFS file storage patterns

---

**Notes:**
- All items are non-blocking for current production release
- Priority levels may change based on user feedback
- Create separate issues/tickets for each item before implementation

**Last Updated:** 2025-12-05
**Version:** v2.0.0-secure-refactor
