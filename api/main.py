from fastapi import FastAPI, Request, Query, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
import json
from typing import Optional, Dict, Any, List
import os
import logging
import time
import asyncio
from contextlib import asynccontextmanager

# Configure logging - minimal for production
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
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

# Response model for payment creation
class PaymentResponse(BaseModel):
    status: str
    correlationId: str
    instance: str = Field(default="unknown")

# Global Redis connection
redis_client = None
# Instance identification
instance_id = os.getenv("INSTANCE", "unknown")

# Use asynccontextmanager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global redis_client
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    
    logger.info(f"API instance {instance_id} starting up, connecting to Redis at {redis_url}")
    
    for attempt in range(5):
        try:
            redis_client = await aioredis.from_url(
                redis_url, 
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_keepalive=True,
                health_check_interval=30.0,
                max_connections=50
            )
            await redis_client.ping()
            logger.info(f"Successfully connected to Redis (attempt {attempt+1})")
            break
        except Exception as e:
            logger.error(f"Failed to connect to Redis (attempt {attempt+1}): {e}")
            if attempt == 4:  # Last attempt
                raise
            await asyncio.sleep(1)
    
    yield
    
    # Shutdown
    logger.info(f"API instance {instance_id} shutting down")
    if redis_client:
        await redis_client.close()
        logger.info("Redis connection closed")

# Create FastAPI app with optimized settings
app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

# Dependency to get Redis connection
async def get_redis():
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis connection not available")
    return redis_client

@app.get("/health", status_code=200)
async def health_check():
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis connection not available")
    return {"status": "ok", "instance": instance_id}

@app.post("/payments", status_code=202, response_model=PaymentResponse)
async def create_payment(payment: PaymentRequest):
    """Queue a payment for processing with better guarantees for consistency."""
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis connection not available")
    
    try:
        # 1. Create a unique processing ID
        processing_id = f"{payment.correlationId}:{instance_id}:{time.time()}"
        
        # 2. First check if this correlation ID is already being processed
        # This prevents duplicate submissions
        exists = await redis_client.exists(f"processing:{payment.correlationId}")
        if exists:
            logger.warning(f"Duplicate payment request detected: {payment.correlationId}")
            return PaymentResponse(
                status="already_queued", 
                correlationId=payment.correlationId,
                instance=instance_id
            )
        
        # 3. Mark this correlation ID as being processed (with 5 minute expiry)
        # This helps prevent race conditions between API instances
        pipe = redis_client.pipeline()
        pipe.set(f"processing:{payment.correlationId}", processing_id, ex=300)
        
        # 4. Add to processing queue with structured data
        task_data = {
            "correlationId": payment.correlationId,
            "amount": payment.amount,
            "processingId": processing_id,
            "timestamp": time.time(),
            "apiInstance": instance_id
        }
        pipe.rpush("payment_queue", json.dumps(task_data))
        
        # 5. Keep track of submitted payments for consistency checks
        pipe.sadd("submitted_payments", payment.correlationId)
        
        # Execute all operations atomically
        await pipe.execute()
        
        return PaymentResponse(
            status="queued", 
            correlationId=payment.correlationId,
            instance=instance_id
        )
    except Exception as e:
        logger.error(f"Error queueing payment: {e}")
        raise HTTPException(status_code=500, detail="Failed to queue payment")

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

@app.get("/payments-summary", response_model=BackendSummary)
async def summary(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    redis: aioredis.Redis = Depends(get_redis)
):
    try:
        # Get consistency metrics
        pipe = redis.pipeline()
        pipe.hget("summary", "success")
        pipe.hget("summary", "success_amount")
        pipe.hget("summary", "fallback")
        pipe.hget("summary", "fallback_amount")
        
        # Also get consistency tracking info
        pipe.scard("submitted_payments")
        pipe.scard("processed_payments")
        
        results = await pipe.execute()

        # Process summary data
        default_requests = int(results[0] or 0)
        default_amount = float(results[1] or 0)
        fallback_requests = int(results[2] or 0)
        fallback_amount = float(results[3] or 0)
        
        # Log consistency info for debugging
        submitted_count = results[4]
        processed_count = results[5]
        if submitted_count != processed_count:
            logger.warning(f"Consistency gap: {submitted_count} submitted vs {processed_count} processed")

        return BackendSummary(
            default=PaymentSummary(totalRequests=default_requests, totalAmount=default_amount),
            fallback=PaymentSummary(totalRequests=fallback_requests, totalAmount=fallback_amount)
        )
    except Exception as e:
        logger.error(f"Error retrieving payment summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve payment summary")

# Additional endpoints for debugging/monitoring
@app.get("/consistency-check", status_code=200)
async def consistency_check(redis: aioredis.Redis = Depends(get_redis)):
    """Check for consistency issues between submitted and processed payments."""
    try:
        # Get submitted and processed payment sets
        submitted = await redis.smembers("submitted_payments")
        processed = await redis.smembers("processed_payments")
        
        # Find missing payments (submitted but not processed)
        missing = set(submitted) - set(processed)
        
        # Find extra payments (processed but not submitted)
        extra = set(processed) - set(submitted)
        
        return {
            "submitted_count": len(submitted),
            "processed_count": len(processed),
            "missing_count": len(missing),
            "extra_count": len(extra),
            "missing_sample": list(missing)[:10] if missing else [],
            "extra_sample": list(extra)[:10] if extra else []
        }
    except Exception as e:
        logger.error(f"Error checking consistency: {e}")
        raise HTTPException(status_code=500, detail="Failed to check consistency")
