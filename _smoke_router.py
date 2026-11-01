#!/usr/bin/env python3
"""Gopher's own Step-1 smoke test for the beansd router (spec-verification).

Seeds daemon.bridges directly (no real bridge spawn) and tests the router:
meta routes from live scan, dispatch by config name + gguf basename, splice
passthrough, 404 model_not_found, thread-per-connection.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import beansd  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %s %s" % (name, extra))
    else:
        FAIL += 1
        print("FAIL %s %s" % (name, extra))


def http_request(port, method, path, body=None, host="127.0.0.1"):
    s = socket.create_connection((host, port), timeout=5)
    s.settimeout(8)
    req = "%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n" % (method, path, host)
    if body is not None:
        req += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(body)
    req += "\r\n"
    if body is not None:
        req += body
    s.sendall(req.encode())
    data = b""
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 1)[1].split(b" ", 1)[0])
    return status, rest


def fake_backend(port):
    """Echo backend: 200 with a marker body for any request received."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                body = b'{"backend":"ok"}'
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def main():
    tmp = tempfile.mkdtemp(prefix="beans-smoke-")
    try:
        tree = os.path.join(tmp, "tree")
        os.makedirs(tree)
        for d, cfg in [
            ("ornith", {"name": "ornith", "ctx": 65535, "file": "ornith.Q.gguf"}),
            ("nomic", {"name": "nomic-embed", "ctx": 8192, "file": "nomic.gguf",
                       "embeddings": True}),
        ]:
            dd = os.path.join(tree, d)
            os.makedirs(dd)
            with open(os.path.join(dd, "config.json"), "w") as f:
                json.dump(cfg, f)
            with open(os.path.join(dd, cfg["file"]), "w") as f:
                f.write("dummy weights")

        daemon = beansd.BeansD(tree, os.path.join(tmp, "cfg"),
                               os.path.join(tmp, "state.json"),
                               listen="127.0.0.1:0")

        # Seed bridge records directly (no spawn): (pid, port, name, gguf)
        daemon.bridges = {
            "ornith": (99991, 19091, "ornith", "ornith.Q.gguf"),
            "nomic": (99992, 19092, "nomic-embed", "nomic.gguf"),
        }
        backends = {19091: fake_backend(19091), 19092: fake_backend(19092)}

        daemon.start_router()
        deadline = time.time() + 3
        while daemon.router_sock is None and time.time() < deadline:
            time.sleep(0.05)
        rport = daemon.router_sock.getsockname()[1]

        # 1. /v1/models merged list from live scan
        st, body = http_request(rport, "GET", "/v1/models")
        models = json.loads(body)["data"]
        ids = {m["id"]: m.get("context_length") for m in models}
        check("GET /v1/models 200", st == 200, str(st))
        check("models merged (2)", len(models) == 2, str(len(models)))
        check("ornith ctx 65535 intact", ids.get("ornith") == 65535, str(ids))
        check("nomic-embed ctx 8192 intact", ids.get("nomic-embed") == 8192, str(ids))

        # 2. /v1/props llama.cpp shape -> CHAT model's n_ctx (not embed's)
        st, body = http_request(rport, "GET", "/v1/props")
        p = json.loads(body)
        check("GET /v1/props 200", st == 200, str(st))
        check("props n_ctx=65535 (chat not embed)",
              p["default_generation_settings"]["n_ctx"] == 65535, str(p))
        check("props model=ornith", p.get("model") == "ornith", str(p.get("model")))

        # 3. /health
        st, body = http_request(rport, "GET", "/health")
        check("GET /health 200 ok", st == 200 and b"ok" in body, str(st))

        # 4. dispatch by config name -> relayed to owning bridge backend
        st, body = http_request(rport, "POST", "/v1/chat/completions",
                                json.dumps({"model": "ornith",
                                            "messages": [{"role": "user", "content": "hi"}]}))
        check("dispatch by config name 200", st == 200, str(st))
        check("relay reached backend", b"backend" in body, str(body[:40]))

        # 5. dispatch by gguf basename
        st, body = http_request(rport, "POST", "/v1/embeddings",
                                json.dumps({"model": "nomic.gguf", "input": ["x"]}))
        check("dispatch by gguf basename 200", st == 200, str(st))

        # 6. unknown model -> 404 model_not_found
        st, body = http_request(rport, "POST", "/v1/chat/completions",
                                json.dumps({"model": "ghost", "messages": []}))
        check("unknown model 404", st == 404, str(st))
        check("404 body model_not_found", b"model_not_found" in body, str(body[:60]))

        # 7. missing model field -> 404
        st, body = http_request(rport, "POST", "/v1/chat/completions",
                                json.dumps({"prompt": "x"}))
        check("missing model field 404", st == 404, str(st))

        # 8. 4-tuple bridge records intact
        check("4-tuple bridge records", all(len(v) == 4 for v in daemon.bridges.values()))

        # 9. concurrent connections (thread-per-connection) — 5 parallel
        results = []

        def hit(i):
            st2, b2 = http_request(rport, "GET", "/v1/models")
            results.append((st2, b2))

        ts = [threading.Thread(target=hit, args=(i,)) for i in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("5 concurrent /v1/models all 200", all(s == 200 for s, _ in results),
              str([s for s, _ in results]))

        for srv in backends.values():
            srv.close()
        print("\n%d passed, %d failed" % (PASS, FAIL))
        sys.exit(1 if FAIL else 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()