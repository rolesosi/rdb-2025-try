from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
import json
import os
import time
import logging
from typing import Optional, List

# Logging
#logging.basicConfig(level=logging.WARNING)
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
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
                REDIS_URL, 
                decode_responses=True, 
                max_connections=50, 
                health_check_interval=30.0,
                retry_on_timeout=True, 
                socket_keepalive=True 
            )
            await redis_client.ping()
            logger.info(f"Connected to Redis (attempt {attempt+1}): {REDIS_URL}")

             # Initialize counters if needed
            await ensure_counters_initialized(redis_client)
            break
            break
        except Exception as e:
            logger.warning(f"Redis connection failed (attempt {attempt+1}): {e}")
            if attempt == 4:
                raise
            time.sleep(1)

async def ensure_counters_initialized(redis):
    """Ensure all required counters and data structures exist."""
    try:
        pipe = redis.pipeline()
        # Check if summary hash exists
        pipe.hexists("summary", "success")
        # Initialize if needed
        if not await pipe.execute()[0]:
            logger.info("Initializing counters")
            init_pipe = redis.pipeline()
            init_pipe.hset("summary", "success", 0)
            init_pipe.hset("summary", "success_amount", 0.0)
            init_pipe.hset("summary", "fallback", 0) 
            init_pipe.hset("summary", "fallback_amount", 0.0)
            await init_pipe.execute()
    except Exception as e:
        logger.warning(f"Failed to initialize counters: {e}")

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
    status = "ok"
    redis_status = "unknown"
    
    if redis_client:
        try:
            # Test Redis connection
            ping_response = await redis_client.ping()
            redis_status = "connected" if ping_response else "error"
        except Exception as e:
            logger.warning(f"Redis health check failed: {e}")
            redis_status = "error"
            status = "degraded"
    else:
        redis_status = "disconnected"
        status = "degraded"
        
    return {
        "status": status,
        "instance": instance_id,
        "redis": redis_status,
        "timestamp": time.time()
    }

# Payment queue
@app.post("/payments", response_model=PaymentResponse, status_code=202)
async def create_payment(payment: PaymentRequest, redis: aioredis.Redis = Depends(get_redis)):
    lock_key = f"processing:{payment.correlationId}"
    lock_set = False
    try:
        # Lock para deduplicar por correlationId
        lock_set = await redis.set(lock_key, f"{instance_id}:{time.time()}", nx=True, ex=300)
        if not lock_set:
            existing_lock = await redis.get(lock_key)
            logger.info(f"Payment already locked: {payment.correlationId}, lock: {existing_lock}")
            return PaymentResponse(
                status="already_locked", 
                correlationId=payment.correlationId, 
                instance=instance_id
            ),409

        task_data = {
            "correlationId": payment.correlationId,
            "amount": payment.amount,
            "processingId": f"{payment.correlationId}:{instance_id}:{time.time()}",
            "timestamp": time.time(),
            "apiInstance": instance_id,
            "status": "pending"  # Add explicit status tracking
        }

        pipe = redis.pipeline()
        pipe.rpush("payment_queue", json.dumps(task_data))
        pipe.sadd("submitted_payments", payment.correlationId)
        pipe.sadd("pending_payments", payment.correlationId)

        await pipe.execute()

        logger.info(f"Payment queued: {payment.correlationId}, amount: {payment.amount}")
        return PaymentResponse(status="queued", correlationId=payment.correlationId, instance=instance_id)

    except Exception as e:
        # Se falhou depois do lock, libere o lock para não travar novas tentativas
        logger.error(f"Error queueing payment: {e}")
        try:
            if lock_set:
                await redis.unlink(lock_key)
        except Exception as cleanup_error:
            logger.error(f"Failed to clean up lock: {cleanup_error}")
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
        pipe.scard("pending_payments")  # Add pending_payments tracking
        results = await pipe.execute()

        default_requests = int(results[0] or 0)
        default_amount = float(results[1] or 0.0)
        fallback_requests = int(results[2] or 0)
        fallback_amount = float(results[3] or 0.0)

        submitted_count = int(results[4] or 0)
        processed_count = int(results[5] or 0)
        failed_count = int(results[6] or 0)
        pending_count = int(results[7] or 0)

        # Gap agora considera sucessos + falhas
        accounted = processed_count + failed_count
        if submitted_count != accounted:
            logger.warning(
                f"Consistency gap: submitted={submitted_count} vs "
                f"accounted(processed={processed_count} + failed={failed_count} + "
                f"pending={pending_count}) = {accounted}"
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
