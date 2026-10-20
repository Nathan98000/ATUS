import threading, time
from psycopg_pool import ConnectionPool, TooManyRequests, PoolTimeout
pool = ConnectionPool("postgresql://atus:atus@localhost:5434/atus", min_size=1, max_size=1, max_waiting=3, open=True)
pool.wait()
held = pool.getconn()
res=[]
def w():
    try: pool.getconn(timeout=0.3)
    except Exception as e: res.append(type(e).__name__)
ts=[threading.Thread(target=w) for _ in range(3)]
[t.start() for t in ts]; [t.join() for t in ts]
print("after 3 timeouts:", res, "requests_waiting=", pool.get_stats().get("requests_waiting"), "pool_available=", pool.get_stats().get("pool_available"))
try: pool.getconn(timeout=5); print("4th got conn")
except Exception as e: print("4th immediate:", type(e).__name__, e)
pool.putconn(held)
print("after return: requests_waiting=", pool.get_stats().get("requests_waiting"), "pool_available=", pool.get_stats().get("pool_available"))
c=pool.getconn(timeout=1); print("now getconn OK"); pool.putconn(c); pool.close()
