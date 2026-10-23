# `sock/` Development Guide

This is a runtime directory for Unix-domain daemon sockets, one `<alias>.sock` per daemon. Clients connect through these sockets after resolving an alias and complete the `hello` handshake before exchanging protocol messages.

Do not hand-edit, inspect as text, or commit socket files. Socket lifecycle belongs in `serial_mux/daemon.py` and CLI/client recovery logic. If socket behavior changes, test stale sockets, daemon shutdown, reconnect, framing, and client recovery without relying on a real developer runtime directory.
