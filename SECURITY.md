# Security

## Reporting

Report vulnerabilities privately via GitHub Security Advisories on this
repository. Please do not open public issues for security reports. You can
expect an acknowledgement within a week.

## Scope and posture

- The plugin's only required credential is a **Discord webhook URL**:
  write-only, bound to a single channel — a leak costs the ability to post in
  that one channel. Rotate the webhook to revoke.
- The optional **bot token** should be scoped to the single forum channel
  (`View Channel` + `Manage Threads`); the plugin only reads one channel
  object and PATCHes threads within it.
- Secrets are read through Hermes's per-profile secret scope, never stored,
  never logged. Every outbound payload neutralizes Discord mentions and is
  deterministically truncated.
- What the plugin publishes (task titles, block reasons, summaries) leaves the
  machine permanently — see the README's privacy section and ADR-0011 for the
  egress boundary.
