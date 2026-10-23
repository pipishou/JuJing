# -*- coding: utf-8 -*-
"""最小 RFC6455 WebSocket 客户端（stdlib only，G1 批次 L0 骑 Octop 用）。

为什么自己写：Octop 的 dashboard 对话发消息只有 WebSocket 通道
（/api/agents/<id>/chat/ws），bot-team 纪律是零新增依赖，故手写。
只覆盖本项目需要的窄场景：明文 ws://、文本帧、单线程顺序收发、
自动回 pong、分片重组、close 帧。不做：wss/TLS、并发、压缩扩展。

口径：本文件只管协议字节；认证与帧语义在 core.octop_client。
"""
import base64
import hashlib
import json
import os
import socket
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class WSError(RuntimeError):
    """握手失败 / 连接中断 / 协议违例。"""


class WSClient:
    """同步阻塞 ws:// 客户端。用法：connect() -> send_text()/recv_text()*N -> close()。"""

    def __init__(self, host, port, path, timeout=180.0):
        self.host = str(host)
        self.port = int(port)
        self.path = str(path or "/")
        self.timeout = float(timeout)
        self._sock = None
        self._buf = b""
        self._closed = False

    # ---- 连接与握手 ----
    def connect(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.sendall(request.encode("ascii"))
            head = self._read_until(b"\r\n\r\n")
        except OSError as exc:
            raise WSError(f"WS 握手连接失败: {exc}") from exc
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in f" {status_line} " and not status_line.startswith("HTTP/1.1 101"):
            raise WSError(f"WS 握手被拒: {status_line}")
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        head_text = head.decode("latin-1")
        if expected not in head_text:
            raise WSError("WS 握手 Sec-WebSocket-Accept 校验失败（对端不是 RFC6455 服务端）")
        return self

    def _read_until(self, marker):
        while marker not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSError("握手阶段连接被对端关闭")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(marker)
        return head + marker

    # ---- 发送（客户端帧必须掩码）----
    def send_text(self, text):
        self._send_frame(_OP_TEXT, str(text).encode("utf-8"))

    def _send_frame(self, opcode, payload):
        if self._sock is None:
            raise WSError("WS 未连接")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n <= 125:
            header.append(0x80 | n)
        elif n <= 0xFFFF:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        try:
            self._sock.sendall(bytes(header) + mask + masked)
        except OSError as exc:
            raise WSError(f"WS 发送失败: {exc}") from exc

    # ---- 接收 ----
    def recv_text(self):
        """收一条完整文本消息（分片自动重组）。连接关闭返回 None；ping 自动回 pong。"""
        frag = None
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                self._closed = True
                try:
                    self._send_frame(_OP_CLOSE, payload[:2] if len(payload) >= 2 else b"")
                except WSError:
                    pass
                self._close_socket()
                return None
            if opcode == _OP_TEXT:
                if frag is not None:
                    raise WSError("协议违例：新文本帧出现在分片中间")
                if fin:
                    return payload.decode("utf-8")
                frag = [payload]
                continue
            if opcode == _OP_CONT:
                if frag is None:
                    raise WSError("协议违例：无起始帧的 continuation")
                frag.append(payload)
                if fin:
                    whole = b"".join(frag)
                    frag = None
                    return whole.decode("utf-8")
                continue
            raise WSError(f"协议违例：未知 opcode 0x{opcode:x}")

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSError("WS 连接中断（对端关闭）")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_frame(self):
        """读一条原始帧。返回 (fin, opcode, payload)。"""
        head = self._recv_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        if masked:  # RFC6455: 服务端帧不该掩码；容忍但剥掉
            mask = self._recv_exact(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exact(length)))
        else:
            payload = self._recv_exact(length)
        return fin, opcode, payload

    # ---- 关闭 ----
    def close(self):
        if self._closed or self._sock is None:
            self._close_socket()
            return
        try:
            self._send_frame(_OP_CLOSE, struct.pack(">H", 1000))
        except WSError:
            pass
        self._closed = True
        self._close_socket()

    def _close_socket(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def turn_once(host, port, path, text, timeout=180.0, done_types=("done",)):
    """一次性问答：连接 -> 发一条 user_turn -> 收帧到 done/close -> 返回 (frames, text)。

    帧语义宽容：done 判定按帧 JSON 的 type 字段；assistant 文本聚合自
    type 含 token/chunk/message/turn 的帧的 text/delta/content 字段（实测
    答案走 token 帧；reasoning 是思维链，在 extract 里排除）。帧结构变化
    只改这里，不碰协议层。
    """
    client = WSClient(host, port, path, timeout=timeout).connect()
    frames = []
    try:
        client.send_text(json.dumps({"type": "user_turn", "text": text,
                                     "conversation_mode": "ask"}, ensure_ascii=False))
        while True:
            raw = client.recv_text()
            if raw is None:
                break
            try:
                frame = json.loads(raw)
            except ValueError:
                frame = {"type": "raw", "text": raw}
            frames.append(frame)
            if isinstance(frame, dict) and str(frame.get("type") or "") in done_types:
                break
    finally:
        client.close()
    return frames, extract_assistant_text(frames)


def extract_assistant_text(frames):
    """从帧序列聚合 assistant 文本。

    实测帧语义（2026-10-23 对活服务裸帧捕获）：
      - 答案增量帧 {"type": "token", "content": "..."}；
      - 思维链帧 {"type": "reasoning", "content": "..."} 必须排除——里面也含
        JSON 草稿，混入会让上层解析到思维链副本；
      - 历史宽容：type 含 chunk/message/turn/delta 的帧照旧采纳。
    """
    parts = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        ftype = str(frame.get("type") or "")
        if "reasoning" in ftype:
            continue
        if not any(tag in ftype for tag in ("token", "chunk", "message", "turn", "delta")):
            continue
        for key in ("text", "delta", "content"):
            value = frame.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
                break
    return "".join(parts)
