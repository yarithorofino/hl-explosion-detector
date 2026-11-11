#!/usr/bin/env python3
"""Listen to Hyperliquid's public trades websocket and store aggressor notional.

candleSnapshot has no buy/sell split. recentTrades is only the last few
prints, so it is not an hour of delta. This process subscribes to `trades`
for the liquid perp universe and writes 15-minute buy/sell notional to
data/delta_tape.json. The detector merges that file into its state.

Coverage for a coin starts when the server acks that subscription, not when
the subscribe message is sent. A name that never gets an ack is not a watched
hour of zero delta.

Side B is the buying aggressor and side A the seller. Spot names are never
subscribed. Webhook secrets are not read and not printed.
"""
import base64
import json
import os
import select
import signal
import socket
import ssl
import struct
import time
from pathlib import Path

import confluence
from perps_all import scan

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "delta_tape.json"
CFG = ROOT / "data" / "detector_config.json"
HOST = "api.hyperliquid.xyz"
PATH = "/ws"


def load_cfg():
    return json.loads(CFG.read_text(encoding="utf-8"))


def universe(cfg):
    snap = scan()
    return [a["coin"] for a in snap.get("assets") or [] if confluence.in_universe(a, cfg)]


def encode_frame(opcode, payload):
    mask = os.urandom(4)
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(raw)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    header += mask
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(raw))
    return bytes(header) + masked


def pop_frames(buffer):
    """Pull complete server frames. Each item is (fin, opcode, payload)."""
    frames = []
    view = buffer
    while len(view) >= 2:
        first, second = view[0], view[1]
        fin = (first & 0x80) != 0
        opcode = first & 0x0F
        masked = (second & 0x80) != 0
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(view) < offset + 2:
                break
            length = struct.unpack("!H", view[offset:offset + 2])[0]
            offset += 2
        elif length == 127:
            if len(view) < offset + 8:
                break
            length = struct.unpack("!Q", view[offset:offset + 8])[0]
            offset += 8
        if length < 0 or length > 8_000_000:
            raise ConnectionError("trama demasiado grande")
        mask = b""
        if masked:
            if len(view) < offset + 4 + length:
                break
            mask = view[offset:offset + 4]
            offset += 4
        elif len(view) < offset + length:
            break
        payload = view[offset:offset + length]
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        frames.append((fin, opcode, payload))
        view = view[offset + length:]
    return frames, view


def connect():
    raw = socket.create_connection((HOST, 443), timeout=20)
    wrapped = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {PATH} HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: hl-explosion-detector/1.0\r\n"
        "\r\n"
    )
    wrapped.sendall(request.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        piece = wrapped.recv(4096)
        if not piece:
            raise ConnectionError("sin respuesta de websocket")
        data += piece
        if len(data) > 8192:
            raise ConnectionError("cabecera de websocket demasiado larga")
    head, _, rest = data.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise ConnectionError("websocket no aceptado")
    return wrapped, rest


class Connection:
    def __init__(self, sock, leftover=b""):
        self.sock = sock
        self.buffer = leftover
        self.parts = []

    def send_json(self, obj):
        self.sock.sendall(encode_frame(0x1, json.dumps(obj)))

    def pull(self, timeout):
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if ready:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket cerrado")
            self.buffer += chunk
        frames, self.buffer = pop_frames(self.buffer)
        texts = []
        for fin, opcode, payload in frames:
            if opcode == 0x9:
                self.sock.sendall(encode_frame(0xA, payload))
                continue
            if opcode == 0x8:
                raise ConnectionError("websocket close")
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                self.parts = [payload]
            elif opcode == 0x0:
                self.parts.append(payload)
            else:
                continue
            if not fin:
                continue
            blob = b"".join(self.parts)
            self.parts = []
            if blob[:1] in (b"{", b"["):
                try:
                    texts.append(json.loads(blob))
                except json.JSONDecodeError:
                    pass
        return texts


def write_snapshot(tape, now_ms):
    payload = tape.snapshot(now_ms)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)


def handle_message(tape, message, now_ms):
    """Apply one websocket message. Coverage starts on the ack only."""
    if not isinstance(message, dict):
        return
    channel = message.get("channel")
    if channel == "subscriptionResponse":
        data = message.get("data")
        if not isinstance(data, dict) or data.get("error"):
            return
        sub = data.get("subscription") or {}
        if sub.get("type") == "trades" and sub.get("coin"):
            tape.note_subscribed(sub["coin"], now_ms)
        return
    if channel != "trades":
        return
    for trade in message.get("data") or []:
        if not isinstance(trade, dict):
            continue
        tape.add_trade(
            trade.get("coin"), trade.get("time"), trade.get("side"),
            trade.get("px"), trade.get("sz"), now_ms,
        )


def listen(names, tape, stop_check):
    sock, leftover = connect()
    try:
        conn = Connection(sock, leftover)
        for coin in names:
            conn.send_json({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}})
        # Do not mark the coin watched here. Until the ack arrives the gap
        # stays uncovered, so a rejected subscribe cannot pass as zero delta.
        last_write = 0.0
        last_ping = time.time()
        while not stop_check():
            now_ms = int(time.time() * 1000)
            for message in conn.pull(5):
                handle_message(tape, message, now_ms)
            now = time.time()
            if now - last_ping >= 20:
                conn.send_json({"method": "ping"})
                last_ping = now
            if now - last_write >= 15:
                write_snapshot(tape, int(now * 1000))
                last_write = now
    finally:
        now_ms = int(time.time() * 1000)
        tape.disconnect(now_ms)
        write_snapshot(tape, now_ms)
        try:
            sock.close()
        except OSError:
            pass


def main():
    stop = {"flag": False}

    def _stop(*_args):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    time.sleep(15)
    cfg = load_cfg()
    tape = confluence.Tape(str(int(time.time() * 1000)))
    while not stop["flag"]:
        try:
            names = universe(cfg)
        except Exception as exc:
            print(f"cinta: no se pudo leer el universo ({exc})", flush=True)
            time.sleep(20)
            continue
        if not names:
            print("cinta: universo vacío, reintento", flush=True)
            time.sleep(20)
            continue
        print(f"cinta: escuchando {len(names)} perps", flush=True)
        try:
            listen(names, tape, lambda: stop["flag"])
        except Exception as exc:
            print(f"cinta: reconexión ({exc})", flush=True)
            now_ms = int(time.time() * 1000)
            tape.disconnect(now_ms)
            write_snapshot(tape, now_ms)
            time.sleep(3)
    print("cinta: parada", flush=True)


if __name__ == "__main__":
    main()
