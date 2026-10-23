import socket, time, subprocess, sys
path=sys.argv[1]
s=socket.create_connection(("127.0.0.1",8437))
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
def act():
    return subprocess.run(["docker","exec","atus-postgres","psql","-U","atus","-d","atus_refute_yield","-tAc","select state, left(query,40) from pg_stat_activity where datname='atus_refute_yield' and pid<>pg_backend_pid()"],capture_output=True,text=True).stdout.strip()
for i in range(4):
    time.sleep(1.5); print(f"t={1.5*(i+1)}s not reading:", act().replace("\n"," | "))
got=0
while True:
    b=s.recv(1<<20)
    if not b: break
    got+=len(b)
    if b"\r\n0\r\n" in b or got>20_000_000: break
time.sleep(1); print("after reading", got, "bytes:", act().replace("\n"," | "))
