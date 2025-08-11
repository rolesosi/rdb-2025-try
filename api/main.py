from fastapi import FastAPI, Request, Query, HTTPException, Depends
from pydantic import BaseModel
import redis.asyncio as aioredis
import json
from typing import Optional, Dict, Any
import os
import logging
import time
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Modelo Pydantic para validação automática do corpo da requisição
class PaymentRequest(BaseModel):
    correlationId: str
    amount: float

# Modelos para a resposta do summary
class PaymentSummary(BaseModel):
    totalRequests: int
    totalAmount: float

class BackendSummary(BaseModel):
    default: PaymentSummary
    fallback: PaymentSummary

# Global Redis connection
redis_client = None

# Use asynccontextmanager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global redis_client
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    instance = os.getenv("INSTANCE", "unknown")
    
    logger.info(f"API instance {instance} starting up, connecting to Redis at {redis_url}")
    
    for attempt in range(5):
        try:
            redis_client = await aioredis.from_url(
                redis_url, 
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_keepalive=True,
                health_check_interval=15.0
            )
            await redis_client.ping()
            logger.info(f"Successfully connected to Redis (attempt {attempt+1})")
            break
        except Exception as e:
            logger.error(f"Failed to connect to Redis (attempt {attempt+1}): {e}")
            if attempt == 4:  # Last attempt
                raise
            time.sleep(1)
    
    yield
    
    # Shutdown
    logger.info(f"API instance {instance} shutting down")
    if redis_client:
        await redis_client.close()
        logger.info("Redis connection closed")

app = FastAPI(lifespan=lifespan)

# Dependency to get Redis connection
async def get_redis():
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis connection not available")
    try:
        await redis_client.ping()
        return redis_client
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        raise HTTPException(status_code=503, detail="Redis service unhealthy")

@app.get("/health", status_code=200)
async def health_check(redis: aioredis.Redis = Depends(get_redis)):
    # This will already check Redis health through the dependency
    instance = os.getenv("INSTANCE", "unknown")
    return {"status": "ok", "instance": instance}

@app.post("/payments", status_code=202)
async def create_payment(
    payment: PaymentRequest, 
    redis: aioredis.Redis = Depends(get_redis)
):
    start_time = time.time()
    instance = os.getenv("INSTANCE", "unknown")
    
    try:
        # Optimized to minimize string operations
        task_string = f"{payment.correlationId}|{payment.amount}"
        await redis.rpush("payment_queue", task_string)
        
        process_time = (time.time() - start_time) * 1000
        logger.info(f"Payment queued: {payment.correlationId} from instance {instance} in {process_time:.2f}ms")
        
        return {
            "status": "queued", 
            "correlationId": payment.correlationId,
            "instance": instance
        }
    except Exception as e:
        logger.error(f"Error queueing payment: {e}")
        raise HTTPException(status_code=500, detail="Failed to queue payment")

@app.post("/purge-payments", status_code=200)
async def purge_payments(redis: aioredis.Redis = Depends(get_redis)):
    try:
        # Use pipeline for atomic operation
        pipe = redis.pipeline()
        pipe.delete("payment_queue")
        pipe.delete("summary")
        await pipe.execute()
        return {"status": "purged"}
    except Exception as e:
        logger.error(f"Error purging payments: {e}")
        raise HTTPException(status_code=500, detail="Failed to purge payments")

@app.get("/payments-summary", response_model=BackendSummary)
async def summary(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    redis: aioredis.Redis = Depends(get_redis)
):
    try:
        # Use pipeline for better performance
        pipe = redis.pipeline()
        pipe.hget("summary", "success")
        pipe.hget("summary", "success_amount")
        pipe.hget("summary", "fallback")
        pipe.hget("summary", "fallback_amount")
        results = await pipe.execute()

        default_requests = int(results[0] or 0)
        default_amount = float(results[1] or 0)
        fallback_requests = int(results[2] or 0)
        fallback_amount = float(results[3] or 0)

        return BackendSummary(
            default=PaymentSummary(totalRequests=default_requests, totalAmount=default_amount),
            fallback=PaymentSummary(totalRequests=fallback_requests, totalAmount=fallback_amount)
        )
    except Exception as e:
        logger.error(f"Error retrieving payment summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve payment summary")