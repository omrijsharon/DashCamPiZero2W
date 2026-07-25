# Version 1 API contract

The public API is rooted at `/api/v1`. `src/dashcam/control/api.py` is the
framework-independent source for endpoint methods, paths, authentication policy,
CSRF requirements, pagination limits, clip-ID parsing, and stable error codes.

All endpoints require an authenticated local session. Every `PUT`, `POST`, or
`DELETE` operation that changes state also requires a server-validated CSRF token.
Preparing the card for removal additionally requires recent reauthentication and
explicit UI confirmation.

Clip paths are never accepted from clients. A canonical UUID `clip_id` is
resolved through the recorder-owned catalog. The recorder returns an approved
pair member under a bounded lease; the WSGI adapter validates the managed
directory/type and releases the lease on completion, error, or early disconnect.
Configuration and status responses expose only an `is_set` indicator for
secrets—never their value, hash, prefix, or reversible fingerprint.

Errors use a closed object with a stable `code`, bounded human-readable `message`,
`retryable` indicator, and optional validated field identifier. Framework/traceback
details are logged through the bounded structured logger and are not returned to
the browser.

Local session, password-verifier, CSRF, recent-reauthentication, rate/size-limit,
strict dispatch, bounded Unix-socket client/server, and WSGI adapter code is
implemented with fakes. Session creation uses:

```text
POST   /api/v1/session
DELETE /api/v1/session
POST   /api/v1/session/reauthenticate
```

The last operation verifies the password again and refreshes the short
prepare-removal authorization window. `POST
/api/v1/system/prepare-sd-removal` additionally requires the exact explicit
confirmation phrase defined by the UI contract.

Binding/server-process selection, socket ownership on the target image, the
phone UI, preview, and the privileged prepare-removal integration remain target
work. No Pi or phone behavior is claimed by the local tests.
