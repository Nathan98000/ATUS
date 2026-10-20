import threading, time
from psycopg_pool import ConnectionPool, TooManyRequests, PoolTimeout
for mw in (0, 3):
    p = ConnectionPool("postgresql://atus:atus@localhost:5434/atus", min_size=1, max_size=1, max_waiting=mw, open=True)
    held = p.getconn()
    res = []
    def w():
        try: p.getconn(timeout=0.3); res.append("ok")
        except TooManyRequests: res.append("TooMany")
        except PoolTimeout: res.append("timeout")
    ts=[threading.Thread(target=w) for _ in range(3)]; [t.start() for t in ts]; [t.join() for t in ts]
    print("max_waiting",mw,"round1",sorted(res),"stats",{k:v for k,v in p.get_stats().items() if k in("pool_available","requests_waiting")})
    res.clear(); t=threading.Thread(target=w); t.start(); t.join()
    print("  after all timed out, new single request ->", res, "requests_waiting", p.get_stats()["requests_waiting"])
    p.putconn(held); time.sleep(0.2)
    print("  after putconn requests_waiting", p.get_stats()["requests_waiting"], "available", p.get_stats()["pool_available"])
    p.close()
