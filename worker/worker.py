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

    for attempt in range(MAX_RETRIES):
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

async def update_stats_and_release_locks(
    redis: aioredis.Redis,
    stats_updates: List[Tuple[bool, str, str, float]],
    locks_to_release: List[str],
):
    if not stats_updates and not locks_to_release:
        return

    pipe = redis.pipeline()
    # Stats
    for success, processor_type, corr_id, amount in stats_updates:
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

    # Libera locks sem bloquear o Redis
    if locks_to_release:
        pipe.unlink(*locks_to_release)

    await pipe.execute()

async def process_batch(session: aiohttp.ClientSession, redis: aioredis.Redis, batch: List[str]):
    if not batch:
        return

    # Parse
    payments: List[PaymentTask] = []
    for task_json in batch:
        try:
            payment = await parse_payment(task_json)
            payments.append(payment)
        except Exception as e:
            print(f"Error parsing payment: {e}")

    if not payments:
        return

    # Default processor (paralelo)
    default_tasks = [process_with_retry(session, DEFAULT_PROCESSOR_URL, p, "default") for p in payments]
    default_results = await asyncio.gather(*default_tasks, return_exceptions=True)

    # Fallback apenas para falhas reais do default
    fallback_indices: List[int] = []
    fallback_tasks: List[asyncio.Task] = []
    for i, result in enumerate(default_results):
        failed = isinstance(result, Exception) or (isinstance(result, tuple) and not result[0])
        if failed:
            fallback_indices.append(i)
            fallback_tasks.append(process_with_retry(session, FALLBACK_PROCESSOR_URL, payments[i], "fallback"))

    fallback_results = await asyncio.gather(*fallback_tasks, return_exceptions=True) if fallback_tasks else []

    # Consolidar resultados finais por pagamento
    stats_updates: List[Tuple[bool, str, str, float]] = []
    locks_to_release: List[str] = []

    fallback_by_index = {fallback_indices[j]: fallback_results[j] for j in range(len(fallback_results))}

    for i, payment in enumerate(payments):
        corr_id = payment["correlationId"]
        amount = float(payment["amount"])
        lock_key = f"processing:{corr_id}"
        locks_to_release.append(lock_key)

        # Resultado default
        default_ok = (not isinstance(default_results[i], Exception)) and bool(default_results[i][0])

        if default_ok:
            # Sucesso no default
            stats_updates.append((True, "default", corr_id, amount))
            continue

        # Tentar fallback (se houver)
        if i in fallback_by_index:
            fb_res = fallback_by_index[i]
            fb_ok = (not isinstance(fb_res, Exception)) and bool(fb_res[0])
            if fb_ok:
                stats_updates.append((True, "fallback", corr_id, amount))
            else:
                # Falha definitiva (default e fallback)
                stats_updates.append((False, "fallback", corr_id, amount))
        else:
            # Falha definitiva (default falhou e não tentou fallback por algum motivo)
            stats_updates.append((False, "default", corr_id, amount))

    await update_stats_and_release_locks(redis, stats_updates, locks_to_release)

    success_count = len([r for r in stats_updates if r[0]])
    failure_count = len([r for r in stats_updates if not r[0]])
    print(f"Processed batch: {success_count} successful, {failure_count} failed, total {len(payments)}")

async def main():
    print("Worker starting...")
    cleanup_timer = 0

    while True:
        redis = None
        try:
            redis = await aioredis.from_url(
                REDIS_URL, decode_responses=True, max_connections=10, health_check_interval=30.0
            )

            conn = aiohttp.TCPConnector(limit=100, limit_per_host=20, enable_cleanup_closed=True)
            async with aiohttp.ClientSession(connector=conn) as session:
                while True:
                    try:
                        current_time = time.time()
                        if current_time - cleanup_timer > 30:
                            # Hook para futuras limpezas de chaves órfãs, se necessário
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
