#!/usr/bin/env python3
"""Send one Telegraf JSON batch over verified HTTPS, without following redirects."""
import argparse
import http.client
import os
import re
import ssl
import sys
from urllib.parse import urlsplit

MAX_BATCH_BYTES = 16 * 1024 * 1024


class DeliveryError(Exception):
    """Messages are deliberately independent of credentials and receiver content."""


def endpoint(value):
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme != "https" or not parts.hostname or port == 0
                or parts.username is not None or parts.password is not None
                or parts.fragment or re.search(r"[\s\x00-\x1f\x7f]", value) or "$" in value):
            raise ValueError
        return parts.hostname, port, parts.path or "/", parts.query
    except ValueError:
        raise DeliveryError("Metric delivery requires a valid HTTPS endpoint without embedded credentials.") from None


def credential():
    token = os.environ.get("SERVER_MONITOR_TOKEN", "")
    if len(token) > 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,}", token):
        raise DeliveryError("Metric delivery requires a valid bearer token in the environment.")
    return token


def deliver(url, token, payload):
    host, port, path, query = endpoint(url)
    if not payload or len(payload) > MAX_BATCH_BYTES:
        raise DeliveryError("Metric batch is empty or exceeds the 16 MiB delivery limit.")
    connection = http.client.HTTPSConnection(host, port, timeout=10, context=ssl.create_default_context())
    try:
        # HTTPSConnection makes exactly one request. It never handles Location
        # headers, so neither credentials nor telemetry can follow a redirect.
        connection.request("POST", path + ("?" + query if query else ""), body=payload,
                           headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            detail = " (redirects are disabled)" if 300 <= response.status < 400 else ""
            raise DeliveryError(f"Metric delivery rejected: HTTP {response.status}{detail}.")
        # Only the status matters. Never read or log the receiver's body or URL.
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--check", action="store_true", help="Validate settings without sending metrics")
    args = parser.parse_args()
    try:
        endpoint(args.url)
        token = credential()
        if args.check:
            ssl.create_default_context()
            return 0
        deliver(args.url, token, sys.stdin.buffer.read(MAX_BATCH_BYTES + 1))
    except DeliveryError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ssl.SSLCertVerificationError:
        print("Metric delivery failed: TLS certificate verification failed.", file=sys.stderr)
        return 1
    except (OSError, ValueError, http.client.HTTPException):
        # Telegraf may put stderr in the journal. Do not print exceptions, which
        # can contain URLs, header values or arbitrary remote response data.
        print("Metric delivery failed: HTTPS connection or response error.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
