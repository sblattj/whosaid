#!/usr/bin/env python3
"""Fake Teams local API (ws://127.0.0.1:<port>) for test/record_app_test.sh (#224).

Accepts one WebSocket client, records the request path, pushes three
messages (muted, unmuted, tokenRefresh), then records every frame the client
sent for 1 s and closes. Prints the port on stdout first; writes
{"path": ..., "client_opcodes": [...]} to OUT_JSON. Stdlib only.

Usage: teams_api_fake_server.py OUT_JSON
"""
import base64
import hashlib
import json
import socket
import struct
import sys
import time

out = sys.argv[1]
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
print(srv.getsockname()[1], flush=True)
srv.settimeout(60)
c, _ = srv.accept()
c.settimeout(10)
req = b""
while b"\r\n\r\n" not in req:
    chunk = c.recv(4096)
    if not chunk:
        sys.exit(1)
    req += chunk
head, rest = req.split(b"\r\n\r\n", 1)
lines = head.decode().split("\r\n")
path = lines[0].split(" ")[1]
key = next(ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sec-websocket-key:"))
accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())


def send(obj) -> None:
    data = json.dumps(obj).encode()
    hdr = bytes([0x81, len(data)]) if len(data) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(data))
    c.sendall(hdr + data)


state = {"isMuted": True, "isInMeeting": True, "isHandRaised": False, "isVideoOn": False}
for msg in ({"meetingUpdate": {"meetingState": state}},
            {"meetingUpdate": {"meetingState": dict(state, isMuted=False)}},
            {"tokenRefresh": "new-token-224"}):
    time.sleep(0.4)
    send(msg)

buf = rest
c.settimeout(1.0)
try:
    while True:
        d = c.recv(4096)
        if not d:
            break
        buf += d
except socket.timeout:
    pass
opcodes = []
i = 0
while i + 2 <= len(buf):
    op, ln = buf[i] & 0x0F, buf[i + 1] & 0x7F
    masked = buf[i + 1] & 0x80
    i += 2
    if ln == 126:
        ln = struct.unpack(">H", buf[i:i + 2])[0]
        i += 2
    elif ln == 127:
        ln = struct.unpack(">Q", buf[i:i + 8])[0]
        i += 8
    i += (4 if masked else 0) + ln
    opcodes.append(op)
c.close()
with open(out, "w") as fh:
    json.dump({"path": path, "client_opcodes": opcodes}, fh)
