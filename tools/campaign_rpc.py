"""Local menu bridge diagnostic client."""
import json
import socket
import sys

def request(command="status"):
    with socket.create_connection(("127.0.0.1", 48779), timeout=12) as sock:
        sock.sendall((command + "\n").encode("utf-8"))
        with sock.makefile("r", encoding="utf-8") as f:
            result = json.loads(f.readline())
    if "error" in result:
        raise RuntimeError(result["error"])
    return result

if __name__ == "__main__":
    print(json.dumps(request(sys.argv[1] if len(sys.argv)>1 else "status"),ensure_ascii=False,indent=2))
