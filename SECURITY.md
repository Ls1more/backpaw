# Security policy

Backpaw runs on every shell command an AI agent makes, outside the agent's sandbox, so security bugs matter.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository.

Include what you ran, what happened, and what you expected. You'll get a reply within a week.

## In scope

- A way to make Backpaw itself do something harmful (for example, a crafted command or log entry that
  makes **Restore** put a file somewhere new, or code execution through the hook).
- A delete or overwrite command that slips past the guard and isn't listed under "Known gaps" in the README.
- Anything that corrupts an agent's config file during install/uninstall.

## Out of scope

The known gaps listed in the README's **Security model** section (script files, shell redirection, an
agent with unrestricted file access editing its own config). Backpaw is a seatbelt, not a sandbox.
