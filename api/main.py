from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
import json
import os
import time
import logging
from typing import Optional, List

# Logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Modelos
class PaymentRequest(BaseModel):
    correlationId: str
    amount: float

class PaymentResponse(BaseModel):
    status: str
    correlationId: str
    instance: str = Field(default="unknown")

class PaymentSummary(BaseModel):
    totalRequests: int
    totalAmount: float

class BackendSummary(BaseModel):
    default: PaymentSummary
    fallback: PaymentSummary

# Globals
redis_client = None
instance_id = os.getenv("INSTANCE", "unknown")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# FastAPI app
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# Startup / shutdown
@app.on_event("startup")
async def startup():
    global redis_client
    for attempt in range(5):
        try:
            redis_client = await aioredis.from_url(
                REDIS_URL, decode_responses=True, max_connections=50, health_check_interval=30.0
            )
            await redis_client.ping()
            logger.info(f"Connected to Redis (attempt {attempt+1})")
            break
        except Exception as e:
            logger.warning(f"Redis connection failed (attempt {attempt+1}): {e}")
            if attempt == 4:
                raise
            time.sleep(1)

@app.on_event("shutdown")
async def shutdown():
    global redis_client
    if redis_client:
        await redis_client.close()
        logger.info("Redis connection closed")

# Dependency
async def get_redis():
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    return redis_client

# Healthcheck
@app.get("/health")
async def health():
    return {"status": "ok", "instance": instance_id}

# Payment queue
@app.post("/payments", response_model=PaymentResponse, status_code=202)
async def create_payment(payment: PaymentRequest, redis: aioredis.Redis = Depends(get_redis)):
    lock_key = f"processing:{payment.correlationId}"
    lock_set = False
    try:
        # Lock para deduplicar por correlationId
        lock_set = await redis.set(lock_key, f"{instance_id}:{time.time()}", nx=True, ex=300)
        if not lock_set:
            return PaymentResponse(status="already_locked", correlationId=payment.correlationId, instance=instance_id)

        task_data = {
            "correlationId": payment.correlationId,
            "amount": payment.amount,
            "processingId": f"{payment.correlationId}:{instance_id}:{time.time()}",
            "timestamp": time.time(),
            "apiInstance": instance_id
        }

        pipe = redis.pipeline()
        pipe.rpush("payment_queue", json.dumps(task_data))
        pipe.sadd("submitted_payments", payment.correlationId)
        await pipe.execute()

        return PaymentResponse(status="queued", correlationId=payment.correlationId, instance=instance_id)

    except Exception as e:
        # Se falhou depois do lock, libere o lock para não travar novas tentativas
        logger.error(f"Error queueing payment: {e}")
        try:
            if lock_set:
                await redis.unlink(lock_key)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to queue payment")

# Summary
@app.get("/payments-summary", response_model=BackendSummary)
async def payments_summary(redis: aioredis.Redis = Depends(get_redis)):
    try:
        pipe = redis.pipeline()
        pipe.hget("summary", "success")
        pipe.hget("summary", "success_amount")
        pipe.hget("summary", "fallback")
        pipe.hget("summary", "fallback_amount")
        pipe.scard("submitted_payments")
        pipe.scard("processed_payments")
        pipe.scard("failed_payments")  # <-- considerar falhas no gap
        results = await pipe.execute()

        default_requests = int(results[0] or 0)
        default_amount = float(results[1] or 0.0)
        fallback_requests = int(results[2] or 0)
        fallback_amount = float(results[3] or 0.0)

        submitted_count = int(results[4] or 0)
        processed_count = int(results[5] or 0)
        failed_count = int(results[6] or 0)

        # Gap agora considera sucessos + falhas
        accounted = processed_count + failed_count
        if submitted_count != accounted:
            logger.warning(
                f"Consistency gap: submitted={submitted_count} vs accounted(processed+failed)={accounted}"
            )

        return BackendSummary(
            default=PaymentSummary(totalRequests=default_requests, totalAmount=default_amount),
            fallback=PaymentSummary(totalRequests=fallback_requests, totalAmount=fallback_amount)
        )
    except Exception as e:
        logger.error(f"Error retrieving summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve summary")

@app.post("/purge-payments", status_code=200)
async def purge_payments(redis: aioredis.Redis = Depends(get_redis)):
    try:
        # Apaga 'processing:*' via SCAN + UNLINK para não bloquear
        to_unlink: List[str] = []
        async for key in redis.scan_iter(match="processing:*", count=100):
            to_unlink.append(key)
            if len(to_unlink) >= 500:
                await redis.unlink(*to_unlink)
                to_unlink.clear()
        if to_unlink:
            await redis.unlink(*to_unlink)

        # Limpa estruturas principais de forma não-bloqueante
        pipe = redis.pipeline()
        pipe.unlink("payment_queue")
        pipe.unlink("summary")
        pipe.unlink("submitted_payments")
        pipe.unlink("processed_payments")
        pipe.unlink("failed_payments")
        await pipe.execute()

        return {"status": "purged"}
    except Exception as e:
        logger.error(f"Error purging payments: {e}")
        raise HTTPException(status_code=500, detail="Failed to purge payments")
