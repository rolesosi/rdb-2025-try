import asyncio
import aiohttp
import json
import sys
import redis.asyncio as aioredis
import os
from typing import List, Dict, Any, Tuple, Optional, Set
import time
import random

# Environment variables
DEFAULT_PROCESSOR_URL = os.getenv("DEFAULT_PROCESSOR_URL")
FALLBACK_PROCESSOR_URL = os.getenv("FALLBACK_PROCESSOR_URL")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Configuration with defaults that can be overridden by environment variables
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "0.5"))
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "5"))
CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=float(os.getenv("HTTP_TIMEOUT", "3")))

# Validate required environment variables
if not DEFAULT_PROCESSOR_URL:
    print("Error: DEFAULT_PROCESSOR_URL environment variable must be defined.")
    sys.exit(1)
if not FALLBACK_PROCESSOR_URL:
    print("Error: FALLBACK_PROCESSOR_URL environment variable must be defined.")
    sys.exit(1)

# Type definitions
PaymentTask = Dict[str, Any]  # Full payment task data
ProcessorResult = Tuple[bool, str, str]  # (success, processor_type, correlation_id)

async def parse_payment(task_json: str) -> PaymentTask:
    """Parse a payment task JSON into a payment data dictionary."""
    try:
        return json.loads(task_json)
    except json.JSONDecodeError:
        # Handle legacy format (correlation_id|amount)
        if '|' in task_json:
            correlation_id, amount_str = task_json.split('|')
            return {
                "correlationId": correlation_id,
                "amount": float(amount_str),
                "timestamp": time.time(),
                "processingId": f"legacy:{correlation_id}:{time.time()}"
            }
        raise

async def process_with_retry(
    session: aiohttp.ClientSession,
    url: str, 
    payment: PaymentTask, 
    processor_type: str,
    max_retries: int = MAX_RETRIES
) -> ProcessorResult:
    """Process a payment with retry logic and exponential backoff."""
    correlation_id = payment["correlationId"]
    payment_data = {"correlationId": correlation_id, "amount": payment["amount"]}
    
    for attempt in range(max_retries):
        try:
            # Exponential backoff with jitter if this is a retry
            if attempt > 0:
                backoff = BACKOFF_BASE * (2 ** (attempt - 1)) * (0.5 + random.random())
                await asyncio.sleep(backoff)
                
            async with session.post(
                f"{url}/payments", 
                json=payment_data, 
                timeout=CLIENT_TIMEOUT
            ) as resp:
                if resp.status == 200:
                     return True, processor_type, correlation_id
                
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
    
    # All attempts failed
    return False, processor_type, correlation_id

async def fetch_batch(redis: aioredis.Redis, batch_size: int) -> List[str]:
    """Fetch a batch of payments from Redis queue."""
    batch = []
    try:
        task_tuple = await redis.blpop("payment_queue", timeout=POLL_TIMEOUT)
        if task_tuple:
            _, task = task_tuple
            batch.append(task)
    except redis.exceptions.ConnectionError:
        return []
    
    if batch:
        remaining = min(batch_size - 1, 50)  # Smaller batch size to process faster
        try:
            pipe = redis.pipeline()
            for _ in range(remaining):
                pipe.lpop("payment_queue")
            results = await pipe.execute()
            batch.extend([r for r in results if r is not None])
        except redis.exceptions.ConnectionError:
            pass
    
    return batch

async def update_stats(
    redis: aioredis.Redis, 
    results: List[Tuple[bool, str, str, float]]
) -> None:
    """Update payment statistics and tracking in Redis atomically."""
    if not results:
        return
    
    # Count successes by processor type
    default_count = sum(1 for success, proc_type, _, _ in results if success and proc_type == "default")
    default_amount = sum(amount for success, proc_type, _, amount in results if success and proc_type == "default")
    fallback_count = sum(1 for success, proc_type, _, _ in results if success and proc_type == "fallback")
    fallback_amount = sum(amount for success, proc_type, _, amount in results if success and proc_type == "fallback")
    
   # Get all correlation IDs that were successfully processed
    successful_correlation_ids = [corr_id for success, _, corr_id, _ in results if success]
    
    # Update stats atomically with pipeline
    try:
        pipe = redis.pipeline()
        
        # Update payment processor stats
        if default_count:
            pipe.hincrby("summary", "success", default_count)
            pipe.hincrbyfloat("summary", "success_amount", default_amount)
        if fallback_count:
            pipe.hincrby("summary", "fallback", fallback_count)
            pipe.hincrbyfloat("summary", "fallback_amount", fallback_amount)
        
        # Critical: Mark all successful payments as processed for consistency tracking
        for corr_id in successful_correlation_ids:
            # Add to processed set
            pipe.sadd("processed_payments", corr_id)
            # Remove processing lock
            pipe.delete(f"processing:{corr_id}")
            
        await pipe.execute()
    except redis.exceptions.ConnectionError:
        print("Failed to update payment statistics")

async def process_batch(
    session: aiohttp.ClientSession,
    redis: aioredis.Redis,
    batch: List[str]
) -> None:
    """Process a batch of payments in parallel."""
    if not batch:
        return
    
    # Parse all payments
    payments = []
    for task_json in batch:
        try:
            payment = await parse_payment(task_json)
            payments.append(payment)
        except Exception as e:
            print(f"Error parsing payment: {e}")
    
    if not payments:
        return
        
    # Process all payments in parallel with default processor
    tasks = []
    for payment in payments:
        tasks.append(
            process_with_retry(
                session, 
                DEFAULT_PROCESSOR_URL, 
                payment, 
                "default"
            )
        )
    
    # Wait for all default processor attempts to complete
    default_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Try fallback processor for failed payments
    fallback_tasks = []
    fallback_payment_indices = []

    for i, result in enumerate(default_results):
        if isinstance(result, Exception) or not result[0]:
            fallback_tasks.append(
                process_with_retry(
                    session, 
                    FALLBACK_PROCESSOR_URL, 
                    payments[i], 
                    "fallback"
                )
            )
            fallback_payment_indices.append(i)

    # Wait for all fallback processor attempts to complete
    fallback_results = await asyncio.gather(*fallback_tasks, return_exceptions=True) if fallback_tasks else []
    
    # Prepare final results for stats update
    stats_updates = []
    
    # Add successful default processor results
    for i, result in enumerate(default_results):
        if not isinstance(result, Exception) and result[0]:
            stats_updates.append((True, "default", result[2], payments[i]["amount"]))
    
    # Add successful fallback processor results
    for j, result in enumerate(fallback_results):
        if not isinstance(result, Exception) and result[0]:
            original_idx = fallback_payment_indices[j]
            stats_updates.append((True, "fallback", result[2], payments[original_idx]["amount"]))
    
    # Update statistics in Redis
    await update_stats(redis, stats_updates)

    # Log processing results
    success_count = len([r for r in stats_updates if r[0]])
    print(f"Processed batch: {success_count}/{len(payments)} successful")

async def cleanup_stale_locks(redis: aioredis.Redis) -> None:
    """Periodically clean up stale processing locks to prevent stuck payments."""
    try:
        # Get all processing keys
        processing_keys = await redis.keys("processing:*")
        
        if not processing_keys:
            return
            
        # Check timestamp for each key
        now = time.time()
        pipe = redis.pipeline()
        
        for key in processing_keys:
            # Get the correlation ID from the key
            correlation_id = key.split(":", 1)[1]
            
            # Check if already processed
            is_processed = await redis.sismember("processed_payments", correlation_id)
            if is_processed:
                # Already processed but lock wasn't cleaned up
                pipe.delete(key)
                continue
                
            # Check if this is a very old lock (>5 minutes)
            # If so, remove it to allow reprocessing
            ttl = await redis.ttl(key)
            if ttl < 0 or ttl < 60:  # No TTL or less than 1 minute left
                pipe.delete(key)
                print(f"Cleaned up stale lock for {correlation_id}")
        
        await pipe.execute()
    except Exception as e:
        print(f"Error cleaning up stale locks: {e}")

async def main():
    """Main worker process."""
    print("Worker starting...")
    cleanup_timer = 0
    
    while True:
        redis = None
        try:
            # Connect to Redis
            redis = await aioredis.from_url(
                REDIS_URL, 
                decode_responses=True,
                max_connections=10,
                health_check_interval=30.0
            )
            
            # Create HTTP session with connection pooling
            conn = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=20,
                enable_cleanup_closed=True
            )
            
            async with aiohttp.ClientSession(connector=conn) as session:
                print("Worker initialized. Processing payments...")
                
                while True:
                    try:
                        # Periodically clean up stale locks (every 30 seconds)
                        current_time = time.time()
                        if current_time - cleanup_timer > 30:
                            await cleanup_stale_locks(redis)
                            cleanup_timer = current_time

                        # Fetch batch of tasks
                        batch = await fetch_batch(redis, BATCH_SIZE)
                        
                        if not batch:
                            await asyncio.sleep(0.01)
                            continue
                            
                        # Process batch of payments
                        await process_batch(session, redis, batch)
                        
                    except Exception as e:
                        print(f"Error processing batch: {e}")
                        await asyncio.sleep(0.5)
                        
        except aioredis.ConnectionError as e:
            print(f"Redis connection error: {e}. Reconnecting...")
            if redis:
                await redis.close()
            await asyncio.sleep(1)
            
        except Exception as e:
            print(f"Unexpected error: {e}. Restarting worker...")
            if redis:
                await redis.close()
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())