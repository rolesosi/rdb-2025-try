from fastapi import FastAPI, Request, Query
import redis.asyncio as aioredis
import uuid, json

app = FastAPI()
redis = None

@app.on_event("startup")
async def startup():
    global redis
    redis = await aioredis.from_url("redis://redis", decode_responses=True)

@app.post("/payments")
async def create_payment(req: Request):
    data = await req.json()
    correlation_id = data.get("correlationId")
    amount = data.get("amount")
    await redis.rpush("payment_queue", json.dumps({ 
        "correlationId": correlation_id,
        "amount": amount
    }))
    return {"status": "queued",  "correlationId": correlation_id}

@app.get("/payments-summary")
async def summary(
    from_: str = Query(None, alias="from"),
    to: str = Query(None)
):
    # Busca os dados do resumo
    default_requests = int(await redis.hget("summary", "success") or 0)
    fallback_requests = int(await redis.hget("summary", "fallback") or 0)
    default_amount = float(await redis.hget("summary", "success_amount") or 0)
    fallback_amount = float(await redis.hget("summary", "fallback_amount") or 0)
    return {
        "default": {
            "totalRequests": default_requests,
            "totalAmount": default_amount
        },
        "fallback": {
            "totalRequests": fallback_requests,
            "totalAmount": fallback_amount
        }
    }