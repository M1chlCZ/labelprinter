import io
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfWriter, PdfReader
from pypdf.generic import ContentStream, DecodedStreamObject, NameObject

import labelprinter


def pdf(width=100, height=150, count=1):
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=width * 72 / 25.4, height=height * 72 / 25.4)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class ConfigTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        for name in list(os.environ):
            if name.startswith("LABEL_"):
                del os.environ[name]
        config = labelprinter.Config.from_environment()
        self.assertEqual((config.listen, config.port), ("127.0.0.1", 8631))
        self.assertEqual(config.queue, "PC43t")
        self.assertEqual(config.state_dir, Path("/var/lib/labelprinter"))
        os.environ["LABEL_LISTEN"] = "0.0.0.0"
        os.environ["LABEL_PORT"] = "9000"
        os.environ["LABEL_QUEUE"] = "Zebra_ZT411"
        os.environ["LABEL_STATE_DIR"] = "/tmp/label-state"
        try:
            config = labelprinter.Config.from_environment()
        finally:
            for name in list(os.environ):
                if name.startswith("LABEL_"):
                    del os.environ[name]
        self.assertEqual((config.listen, config.port), ("0.0.0.0", 9000))
        self.assertEqual(config.queue, "Zebra_ZT411")
        self.assertEqual(config.state_dir, Path("/tmp/label-state"))

    def test_invalid_port_is_rejected(self):
        os.environ["LABEL_PORT"] = "not-a-port"
        try:
            with self.assertRaises(ValueError):
                labelprinter.Config.from_environment()
        finally:
            del os.environ["LABEL_PORT"]
        os.environ["LABEL_PORT"] = "70000"
        try:
            with self.assertRaises(ValueError):
                labelprinter.Config.from_environment()
        finally:
            del os.environ["LABEL_PORT"]


class LabelTests(unittest.TestCase):
    def test_compressed_label_rejection_preserves_next_submission(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=100 * 72 / 25.4, height=150 * 72 / 25.4)
        stream = DecodedStreamObject()
        stream.set_data(b"q\nQ\n" * 1_000_000)
        page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
        output = io.BytesIO()
        writer.write(output)
        self.assertLess(len(output.getvalue()), 5000)
        with tempfile.TemporaryDirectory() as directory:
            submit = Mock(return_value=42)
            spooler = labelprinter.Spooler(Path(directory), submit)
            key = '11111111-1111-4111-8111-111111111111'
            with self.assertRaises(ValueError):
                spooler.print(key, output.getvalue())
            submit.assert_not_called()
            self.assertIsNone(spooler.db.execute("SELECT id FROM receipts").fetchone())
            self.assertEqual(spooler.print(key, pdf()), {'state': 'submitted', 'job_id': 42})
            self.assertEqual(spooler.print(key, pdf()), {'state': 'submitted', 'job_id': 42})
            submit.assert_called_once()
            self.assertFalse(list(Path(directory).glob('*.pdf')))

    def test_stalled_tls_handshake_does_not_block_next_client(self):
        with tempfile.TemporaryDirectory() as directory:
            key, cert = Path(directory) / 'key.pem', Path(directory) / 'cert.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1',
                            '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
            server = labelprinter.TLSServer(('127.0.0.1', 0), labelprinter.Handler)
            server.handshake_timeout = 0.1
            server.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.tls.load_cert_chain(cert, key)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=2):
                    time.sleep(0.3)
                # The stalled connection was dropped by the timeout; give the
                # next client a realistic handshake budget so the assertion
                # does not race the timeout on a loaded machine.
                server.handshake_timeout = 5
                context = ssl.create_default_context(cafile=str(cert))
                with self.assertRaises(urllib.error.HTTPError) as response:
                    urllib.request.urlopen('https://127.0.0.1:%d/' % server.server_port, context=context, timeout=2)
                self.assertEqual(response.exception.code, 501)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_normalizes_a7_without_upscaling(self):
        result = labelprinter.normalize_pdf(pdf(105, 74))
        page = PdfReader(io.BytesIO(result)).pages[0]
        self.assertAlmostEqual(float(page.mediabox.width), 100 * 72 / 25.4, places=3)
        self.assertAlmostEqual(float(page.mediabox.height), 150 * 72 / 25.4, places=3)

    def test_normalization_preserves_vector_label_content(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=100 * 72 / 25.4, height=150 * 72 / 25.4)
        stream = DecodedStreamObject()
        stream.set_data(b"q 0 0 0 rg 10 20 30 40 re f Q\n")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        result = PdfReader(io.BytesIO(labelprinter.normalize_pdf(output.getvalue())))
        content = ContentStream(result.pages[0].get_contents(), result)
        rectangles = [operands for operands, operator in content.operations if operator == b"re"]
        self.assertIn([10, 20, 30, 40], rectangles)
        self.assertIn(b"f", [operator for _, operator in content.operations])

    def test_rejects_multiple_labels_and_a4(self):
        for data in (pdf(count=2), pdf(210, 297), b'^XA^XZ'):
            with self.assertRaises(ValueError):
                labelprinter.normalize_pdf(data)

    def test_replayed_request_submits_once_and_rejects_changed_document(self):
        with tempfile.TemporaryDirectory() as directory:
            submit = Mock(return_value=42)
            spooler = labelprinter.Spooler(Path(directory), submit)
            key = '11111111-1111-4111-8111-111111111111'
            first = spooler.print(key, pdf())
            self.assertEqual(first, spooler.print(key, pdf()))
            self.assertEqual(first['job_id'], 42)
            submit.assert_called_once()
            with self.assertRaises(ValueError):
                spooler.print(key, pdf(74, 105))

    def test_uncertain_submission_is_never_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            submit = Mock(side_effect=TimeoutError())
            spooler = labelprinter.Spooler(Path(directory), submit)
            key = '11111111-1111-4111-8111-111111111111'
            self.assertEqual(spooler.print(key, pdf())['state'], 'unknown')
            self.assertEqual(spooler.print(key, pdf())['state'], 'unknown')
            submit.assert_called_once()

    def test_cups_submit_uses_the_configured_queue(self):
        config = labelprinter.Config(queue="Zebra_ZT411", media="Label100x150", job_prefix="lp-")
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            original = labelprinter.subprocess.run

            def fake_run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="request id is Zebra_ZT411-7\n", stderr="")

            labelprinter.subprocess.run = fake_run
            try:
                job_id = labelprinter.cups_submit("key-1", str(Path(directory) / "label.pdf"), config)
            finally:
                labelprinter.subprocess.run = original
            self.assertEqual(job_id, 7)
            self.assertIn("Zebra_ZT411", calls[0])
            self.assertIn("media=Label100x150", calls[0])
            self.assertIn("lp-key-1", calls[0])


if __name__ == '__main__':
    unittest.main()
