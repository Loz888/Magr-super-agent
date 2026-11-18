"""A real loopback disconnect after one Coding model request is received."""

import socket
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
from sa22_coding_support import coding_environment


def test_f08_coding_http_loss_after_receive_retains_unknown_without_resend(engine, tmp_path):
    received = []
    sent = []

    class AcceptThenDisconnect(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["Content-Length"])
            body = self.rfile.read(size)
            received.append((self.path, len(body), sha256(body).hexdigest()))
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), AcceptThenDisconnect)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    env = None
    try:
        with httpx.Client(trust_env=False, timeout=3) as peer:

            def local_supplier(request):
                sent.append((len(request.content), sha256(request.content).hexdigest()))
                # The registered test transport only forwards the exact request
                # body to this owned loopback peer. No vendor connection or key.
                return peer.post(
                    f"http://127.0.0.1:{server.server_port}/completions",
                    content=request.content,
                )

            env = coding_environment(engine, tmp_path, supplier_hook=local_supplier)
            target = env.experiment.submit(env.input_id)
            result = env.experiment.run_one()
            assert result and not result["changed"] and not result["business_task_completed"]
            assert len(sent) == 1
            assert received == [("/completions", *sent[0])]
            assert sent[0][0] > 0
            assert env.experiment.submit(env.input_id)["run_id"] == target["run_id"]
            assert env.experiment.run_one() is None
            assert len(sent) == len(received) == 1
            with engine.transaction() as conn:
                [usage] = conn.execute(
                    "SELECT status,reserved_micros,settled_micros FROM app.usage_calls "
                    "WHERE run_id=%s",
                    (target["run_id"],),
                ).fetchall()
                assert usage["status"] == "unknown" and usage["reserved_micros"] > 0
                assert usage["settled_micros"] is None
                assert conn.execute(
                    "SELECT state FROM app.sa_coding_operations WHERE run_id=%s AND kind='model'",
                    (target["run_id"],),
                ).fetchall() == [{"state": "unknown"}]
                assert conn.execute(
                    "SELECT state FROM app.sa_scheduler_assignments WHERE run_id=%s",
                    (target["run_id"],),
                ).fetchall() == [{"state": "released"}]
                assert (
                    conn.execute(
                        "SELECT 1 FROM app.sa_coding_verifications "
                        "WHERE coding_id=%s AND result='passed'",
                        (target["coding_id"],),
                    ).fetchone()
                    is None
                )
    finally:
        if env is not None:
            env.close()
        server.shutdown()
        server.server_close()
        worker.join(5)
