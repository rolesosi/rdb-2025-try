from fastapi import FastAPI, HTTPException, Depends, Query
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
import json
import os
import time
import logging
from typing import Optional

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
    try:
        lock_key = f"processing:{payment.correlationId}"
        # Tenta criar o lock
        acquired = await redis.set(lock_key, f"{instance_id}:{time.time()}", nx=True, ex=300)
        if not acquired:
            return PaymentResponse(status="already_locked", correlationId=payment.correlationId, instance=instance_id)

        # Cria tarefa
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
        logger.error(f"Error queueing payment: {e}")
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
        results = await pipe.execute()

        default_requests = int(results[0] or 0)
        default_amount = float(results[1] or 0)
        fallback_requests = int(results[2] or 0)
        fallback_amount = float(results[3] or 0)

        submitted_count = results[4]
        processed_count = results[5]
        if submitted_count != processed_count:
            logger.warning(f"Consistency gap: {submitted_count} submitted vs {processed_count} processed")

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
        # Use pipeline for atomic operation with more keys to purge
        pipe = redis.pipeline()
        
        # Get all processing keys to delete
        processing_keys = await redis.keys("processing:*")
        if processing_keys:
            pipe.delete(*processing_keys)
        
        # Delete all tracking data
        pipe.delete("payment_queue")
        pipe.delete("summary")
        pipe.delete("submitted_payments")
        pipe.delete("processed_payments")
        
        await pipe.execute()
        return {"status": "purged"}
    except Exception as e:
        logger.error(f"Error purging payments: {e}")
        raise HTTPException(status_code=500, detail="Failed to purge payments")