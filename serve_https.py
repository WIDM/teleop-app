"""HTTPS 靜態頁伺服器（port 8443）。

手機的方向感測器（DeviceOrientation、AbsoluteOrientationSensor）只給 HTTPS 頁面用，
所以 orientation.html 不能用 http.server 開。第一次執行會用 openssl 產自簽憑證
（.cert/ 目錄，已加進 .gitignore）；手機瀏覽器會警告憑證不受信任，按「仍要前往」。
"""

import http.server
import pathlib
import ssl
import subprocess
import sys

PORT = 8443
CERT_DIR = pathlib.Path(__file__).parent / ".cert"
CERT, KEY = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"


def ensure_cert():
    if CERT.exists() and KEY.exists():
        return
    CERT_DIR.mkdir(exist_ok=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(KEY),
            "-out",
            str(CERT),
            "-days",
            "365",
            "-subj",
            "/CN=teleop-app-dev",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"已產生自簽憑證：{CERT_DIR}")


def main():
    ensure_cert()
    handler = http.server.SimpleHTTPRequestHandler
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT), str(KEY))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"https://0.0.0.0:{PORT}/orientation.html（手機用電腦的區網 IP）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
