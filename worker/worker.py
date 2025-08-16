import asyncio
import aiohttp
import json
import os
import time
import random
import redis.asyncio as aioredis
from typing import List, Dict, Any, Tuple

# Environment variables
DEFAULT_PROCESSOR_URL = os.getenv("DEFAULT_PROCESSOR_URL")
FALLBACK_PROCESSOR_URL = os.getenv("FALLBACK_PROCESSOR_URL")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Configuration
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "0.5"))
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "5"))
CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=float(os.getenv("HTTP_TIMEOUT", "3")))

# Type definitions
PaymentTask = Dict[str, Any]  # Full payment task data
ProcessorResult = Tuple[bool, str, str]  # (success, processor_type, correlation_id)

# --- Helper functions ---

async def parse_payment(task_json: str) -> PaymentTask:
    try:
        return json.loads(task_json)
    except json.JSONDecodeError:
        # Legacy format
        if "|" in task_json:
            correlation_id, amount_str = task_json.split("|")
            return {
                "correlationId": correlation_id,
                "amount": float(amount_str),
                "timestamp": time.time(),
                "processingId": f"legacy:{correlation_id}:{time.time()}",
                "attempts": 0
            }
        raise

async def process_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    payment: PaymentTask,
    processor_type: str
) -> ProcessorResult:
    correlation_id = payment["correlationId"]
    payment_data = {"correlationId": correlation_id, "amount": payment["amount"]}

    for attempt in range(payment.get("attempts", 0), MAX_RETRIES):
        try:
            if attempt > 0:
                backoff = BACKOFF_BASE * (2 ** attempt) * (0.5 + random.random())
                await asyncio.sleep(backoff)

            async with session.post(f"{url}/payments", json=payment_data, timeout=CLIENT_TIMEOUT) as resp:
                if resp.status == 200:
                    return True, processor_type, correlation_id
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue

    return False, processor_type, correlation_id

async def fetch_batch(redis: aioredis.Redis, batch_size: int) -> List[str]:
    batch = []
    task_tuple = await redis.blpop("payment_queue", timeout=POLL_TIMEOUT)
    if task_tuple:
        _, task = task_tuple
        batch.append(task)

        remaining = min(batch_size - 1, 50)
        pipe = redis.pipeline()
        for _ in range(remaining):
            pipe.lpop("payment_queue")
        results = await pipe.execute()
        batch.extend([r for r in results if r is not None])
    return batch

async def update_stats(redis: aioredis.Redis, results: List[Tuple[bool, str, str, float]]):
    if not results:
        return

    pipe = redis.pipeline()
    for success, processor_type, corr_id, amount in results:
        if success:
            pipe.sadd("processed_payments", corr_id)
            if processor_type == "default":
                pipe.hincrby("summary", "success", 1)
                pipe.hincrbyfloat("summary", "success_amount", amount)
            elif processor_type == "fallback":
                pipe.hincrby("summary", "fallback", 1)
                pipe.hincrbyfloat("summary", "fallback_amount", amount)
        else:
            pipe.sadd("failed_payments", corr_id)
    await pipe.execute()

async def process_batch(session: aiohttp.ClientSession, redis: aioredis.Redis, batch: List[str]):
    if not batch:
        return

    payments = []
    for task_json in batch:
        try:
            payment = await parse_payment(task_json)
            payments.append(payment)
        except Exception as e:
            print(f"Error parsing payment: {e}")

    if not payments:
        return

    # Process default processor
    tasks = [process_with_retry(session, DEFAULT_PROCESSOR_URL, p, "default") for p in payments]
    default_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process fallback for failures
    fallback_tasks = []
    fallback_indices = []
    for i, result in enumerate(default_results):
        if isinstance(result, Exception) or not result[0]:
            fallback_tasks.append(process_with_retry(session, FALLBACK_PROCESSOR_URL, payments[i], "fallback"))
            fallback_indices.append(i)

    fallback_results = await asyncio.gather(*fallback_tasks, return_exceptions=True) if fallback_tasks else []

    stats_updates = []

    # Default successes
    for i, result in enumerate(default_results):
        if not isinstance(result, Exception) and result[0]:
            stats_updates.append((True, "default", result[2], payments[i]["amount"]))
        else:
            payments[i]["attempts"] = payments[i].get("attempts", 0) + 1
            if payments[i]["attempts"] >= MAX_RETRIES:
                stats_updates.append((False, "default", payments[i]["correlationId"], payments[i]["amount"]))

    # Fallback successes
    for j, result in enumerate(fallback_results):
        original_idx = fallback_indices[j]
        if not isinstance(result, Exception) and result[0]:
            stats_updates.append((True, "fallback", result[2], payments[original_idx]["amount"]))
        else:
            payments[original_idx]["attempts"] = payments[original_idx].get("attempts", 0) + 1
            if payments[original_idx]["attempts"] >= MAX_RETRIES:
                stats_updates.append((False, "fallback", payments[original_idx]["correlationId"], payments[original_idx]["amount"]))

    await update_stats(redis, stats_updates)

    success_count = len([r for r in stats_updates if r[0]])
    failure_count = len([r for r in stats_updates if not r[0]])
    print(f"Processed batch: {success_count} successful, {failure_count} failed, total {len(payments)}")

async def main():
    print("Worker starting...")
    cleanup_timer = 0

    while True:
        redis = None
        try:
            redis = await aioredis.from_url(REDIS_URL, decode_responses=True, max_connections=10, health_check_interval=30.0)

            conn = aiohttp.TCPConnector(limit=100, limit_per_host=20, enable_cleanup_closed=True)
            async with aiohttp.ClientSession(connector=conn) as session:
                while True:
                    try:
                        current_time = time.time()
                        if current_time - cleanup_timer > 30:
                            # Optionally, implement cleanup_stale_locks(redis)
                            cleanup_timer = current_time

                        batch = await fetch_batch(redis, BATCH_SIZE)
                        if not batch:
                            await asyncio.sleep(0.01)
                            continue

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
