# `.claude/` Development Guide

This directory contains local assistant settings, not application code. Do not place runtime state, credentials, serial output, or project behavior here.

Assistant changes must follow the repository development contract in the root `agents.md` and the nearest source/test guide. Prefer dedicated file tools for source edits, keep changes narrow, run focused tests followed by the full suite, and update synchronized documentation for user-visible behavior.

Do not weaken test isolation, bypass protocol/state invariants, or use unsigned commits. Repository commits require both DCO and GPG signatures: `git commit -s -S -m "..."`.
