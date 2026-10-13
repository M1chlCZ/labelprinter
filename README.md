# labelprinter

An authenticated, idempotent label printing bridge for CUPS hosts.

A small HTTPS service accepts shipping-label PDFs and hands them to a local
CUPS queue. Every request carries an idempotency key, so a client retry can
never print the same label twice, and an interrupted submission is reported
as `unknown` instead of being retried. CUPS owns the printer; this bridge
only validates, normalizes and delivers the document.

Typical use: a warehouse or ERP system prints labels on a printer that sits
on a private network, without exposing the printer or giving the caller
access to the print host.

## How it works

1. The client sends `POST /jobs/{uuid}` with `Authorization: Bearer TOKEN`
   and `Content-Type: application/pdf` (at most 4 MiB).
2. The bridge validates and normalizes the PDF in a disposable worker
   process with memory, CPU and output limits: exactly one unencrypted
   label page between 30 mm and 160 mm, rotated upright and fitted to
   100 x 150 mm without upscaling.
3. The normalized document is submitted to CUPS (`lp`) once per key. The
   receipt (key, SHA-256 digest, state, CUPS job ID) is stored in a local
   SQLite database with full synchronization.
4. Replaying the same key and document returns the stored receipt without
   printing. Reusing a key with a different document is rejected. A crash
   between the SQLite write and the CUPS result becomes `unknown` and is
   never retried automatically.

## Requirements

- Python 3.11 or later and `pypdf`.
- CUPS client tools (`lp`) on the host.
- A TLS certificate and a shared token of at least 32 characters.

## Install

```sh
sudo ./install.sh
```

The script installs the service under `/usr/local/lib/labelprinter`,
creates the `labelprinter` system user (member of `lpadmin`), installs the
systemd unit and leaves an environment file in `/etc/labelprinter`. Then:

1. Write a token of at least 32 characters to `/etc/labelprinter/token`
   (mode 600, owner `labelprinter`).
2. Put a certificate and key in `/etc/labelprinter/server.crt` and
   `/etc/labelprinter/server.key`.
3. `systemctl enable --now labelprinter`.

The unit denies network and filesystem access by default and allows only
localhost; add your LAN to `IPAddressAllow` to accept remote clients.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LABEL_LISTEN` | `127.0.0.1` | Bind address. |
| `LABEL_PORT` | `8631` | Bind port. |
| `LABEL_QUEUE` | `PC43t` | CUPS queue name. |
| `LABEL_MEDIA` | `Label100x150` | CUPS media option passed to `lp`. |
| `LABEL_JOB_PREFIX` | `labelprinter-` | Prefix for the CUPS job title. |
| `LABEL_TOKEN_FILE` | `/etc/labelprinter/token` | Bearer token file. |
| `LABEL_CERT_FILE` | `/etc/labelprinter/server.crt` | TLS certificate chain. |
| `LABEL_KEY_FILE` | `/etc/labelprinter/server.key` | TLS private key. |
| `LABEL_STATE_DIR` | `/var/lib/labelprinter` | SQLite receipts directory. |

## API

| Request | Response |
| --- | --- |
| `POST /jobs/{uuid}` with a label PDF | `200 {"state": "submitted", "job_id": 42}` |
| Replay of the same key and document | `200` with the stored receipt |
| Same key, different document | `422` |
| Missing or wrong bearer token | `403` |
| Unknown path or malformed key | `404` |
| Not a single label PDF | `422` |
| Truncated body | `400` |

States: `submitted` (CUPS accepted the job) and `unknown` (the outcome
across the CUPS boundary is unknown; never retried automatically).

## The PC43t filter bundle

`pc43-filter`, `pc43.ppd` and `pbmtodp.c` turn a 100 x 150 mm label PDF
into Honeywell Direct Protocol (PRBUF) for a PC43t at 300 dpi. They are
self-contained: `pc43-filter` calls `pbmtodp` from its own directory, so
the bundle works from a checkout and from `/usr/local/lib/labelprinter`.
Install a queue with:

```sh
sudo lpadmin -p PC43t -P pc43.ppd -v usb://... -E
```

The filter needs `poppler-utils` and `netpbm` at run time. `pbmtodp.c` is
third-party software; see THIRD_PARTY_NOTICES.md.

## Security notes

- The server never logs request bodies, tokens or labels.
- Document parsing runs in a child process with `RLIMIT_AS`, `RLIMIT_CPU`
  and `RLIMIT_FSIZE` limits, applied on a best-effort basis where the
  platform cannot enforce them.
- TLS 1.2 or later is required; stalled handshakes cannot pin the server.
- All responses are `Cache-Control: no-store`.

## Development

```sh
python3 -m pip install pypdf
cc -O2 -o pbmtodp pbmtodp.c
LABELPRINTER_PBMTODP=$PWD/pbmtodp python3 -B -m unittest discover -s tests -v
```

The filter tests need `poppler-utils` and `netpbm`; they skip when the
tools or the `pbmtodp` binary are missing.

## License

MIT. See LICENSE. `pbmtodp.c` is BSD 3-Clause (Intermec Technologies);
see THIRD_PARTY_NOTICES.md.
