import psycopg
from fastapi import FastAPI, Depends
from psycopg_pool import ConnectionPool
from atus_pipeline.api.dependencies import get_connection
app = FastAPI()
app.state.pool = ConnectionPool("postgresql://atus:atus@localhost:5434/atus_refute_yield", min_size=1, max_size=2, open=True)
@app.get("/big")
def big(conn: psycopg.Connection = Depends(get_connection)):
    conn.execute("select 1 /*marker*/").fetchone()
    return {"data": "x" * 20_000_000}
@app.get("/small")
def small(conn: psycopg.Connection = Depends(get_connection)):
    conn.execute("select 2 /*marker*/").fetchone()
    return {"data": "x"}
@app.middleware("http")
async def mw(request, call_next):
    return await call_next(request)
