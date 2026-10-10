# Security Policy

## Reporting a vulnerability

Please report suspected security vulnerabilities **privately**. Do not open a
public issue, pull request or discussion for one.

Use GitHub's private vulnerability reporting for this repository: open the
**Security** tab, choose **Advisories**, and select **Report a vulnerability**.
That creates a private security advisory that only the maintainers and you can
see, and the discussion and any fix are coordinated there.

A useful report says which component and file or endpoint is affected, how to
reproduce the problem, what an attacker gains, and which commit or deployment
you tested. Do not include real credentials, customer data, or geological data
you are not entitled to share; if a credential has been exposed (in the
repository, its history, or a log), say so, so that it can be rotated — the
procedures are in [`ops/runbooks/secret-rotation.md`](ops/runbooks/secret-rotation.md).

No response-time commitment is published.

## Supported versions

The project has not made any commitment to support, or to backport security
fixes to, any particular version or release, so there is no supported-versions
table. Please report against the current default branch (`main`).

## Security posture

How the domain service enforces tenant isolation and service-to-service
authentication, and which settings control it, is documented in
[`src/fastapi/SECURITY.md`](src/fastapi/SECURITY.md).
