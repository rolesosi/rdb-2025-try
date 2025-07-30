from fastapi import FastAPI, Request
import aioredis
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
    payment_id = str(uuid.uuid4())
    await redis.rpush("payment_queue", json.dumps({ "id": payment_id, "amount": data["amount"] }))
    return {"status": "queued", "id": payment_id}

@app.get("/payments-summary")
async def summary():
    summary = await redis.hgetall("summary")
    return summary or {"success": 0, "fallback": 0}