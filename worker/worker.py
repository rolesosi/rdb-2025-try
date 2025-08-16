import asyncio
import aiohttp
import json
import os
import time
import random
import redis.asyncio as aioredis
import logging
from typing import List, Dict, Any, Tuple, Optional

# --- Logging setup ---
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("payment_worker")

# Environment variables
DEFAULT_PROCESSOR_URL = os.getenv("DEFAULT_PROCESSOR_URL")
FALLBACK_PROCESSOR_URL = os.getenv("FALLBACK_PROCESSOR_URL")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
WORKER_ID = os.getenv("WORKER_ID", f"worker-{random.randint(1000, 9999)}")

# Configuration
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "0.5"))
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "5"))
CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=float(os.getenv("HTTP_TIMEOUT", "3")))

# Type definitions
PaymentTask = Dict[str, Any]  # Full payment task data
ProcessorResult = Tuple[bool, str, str]  # (success, processor_type, correlation_id)

# Globals
redis_client: Optional[aioredis.Redis] = None
health_info = {
    "start_time": time.time(),
    "processed_count": 0,
    "success_count": 0,
    "failure_count": 0,
    "last_activity": time.time(),
    "status": "starting"
}

# --- Helper functions ---

async def parse_payment(task_json: str) -> PaymentTask:
    try:
        return json.loads(task_json)
    except json.JSONDecodeError:
        # Legacy format
        if "|" in task_json:
            correlation_id, amount_str = task_json.split("|")
            logger.warning(f"Parsed legacy payment format: {correlation_id}")
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
                logger.debug(f"Retry {attempt} for {correlation_id} with {processor_type}")

            async with session.post(f"{url}/payments", json=payment_data, timeout=CLIENT_TIMEOUT) as resp:
                if resp.status == 200:
                    logger.debug(f"Success processing {correlation_id} with {processor_type}")
                    return True, processor_type, correlation_id
                else:
                    response_text = await resp.text()
                    logger.warning(
                        f"Failed to process {correlation_id} with {processor_type}. "
                        f"Status: {resp.status}, Response: {response_text[:100]}"
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError)as e:
            logger.warning(f"Connection error for {correlation_id} with {processor_type}: {str(e)}")
            continue
        except Exception as e:
            logger.error(f"Unexpected error processing {correlation_id} with {processor_type}: {e}", exc_info=True)
            continue

    logger.error(f"All retries failed for {correlation_id} with {processor_type}")
    return False, processor_type, correlation_id

async def fetch_batch(redis: aioredis.Redis, batch_size: int) -> List[str]:
    batch = []
    try:
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

            logger.debug(f"Fetched batch of {len(batch)} payments")
    except Exception as e:
        logger.error(f"Error fetching batch: {e}", exc_info=True)
    return batch

async def update_stats_and_release_locks(
    redis: aioredis.Redis,
    stats_updates: List[Tuple[bool, str, str, float]],
    locks_to_release: List[str],
     pending_to_remove: List[str]
):
    if not stats_updates and not locks_to_release and not pending_to_remove:
        return
    try:
        pipe = redis.pipeline()
        # Stats
        for success, processor_type, corr_id, amount in stats_updates:
            pipe.srem("pending_payments", corr_id)
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
        # Remove any additional pending payments (for cleanup)
            if pending_to_remove:
                pipe.srem("pending_payments", *pending_to_remove)

        await pipe.execute()
        logger.debug(f"Updated stats for {len(stats_updates)} payments, released {len(locks_to_release)} locks")
    except Exception as e:
        logger.error(f"Error updating stats: {e}", exc_info=True)


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
    pending_to_remove: List[str] = []

    fallback_by_index = {fallback_indices[j]: fallback_results[j] for j in range(len(fallback_results))}

    for i, payment in enumerate(payments):
        corr_id = payment["correlationId"]
        amount = float(payment["amount"])
        lock_key = f"processing:{corr_id}"
        locks_to_release.append(lock_key)
        pending_to_remove.append(corr_id)

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

    await update_stats_and_release_locks(redis, stats_updates, locks_to_release, pending_to_remove)

    # Update health metrics
    global health_info
    health_info["processed_count"] += len(payments)
    health_info["success_count"] += len([r for r in stats_updates if r[0]])
    health_info["failure_count"] += len([r for r in stats_updates if not r[0]])
    health_info["last_activity"] = time.time()

    success_count = len([r for r in stats_updates if r[0]])
    failure_count = len([r for r in stats_updates if not r[0]])
    print(f"Processed batch: {success_count} successful, {failure_count} failed, total {len(payments)}")

async def check_orphaned_payments(redis: aioredis.Redis):
    """Check for payments that have been in pending state for too long."""
    try:
        # Identify orphaned payments (pending but no active lock)
        orphaned = []
        
        # Get all pending payments
        pending = await redis.smembers("pending_payments")
        if not pending:
            return
            
        # Check which ones don't have active locks
        pipe = redis.pipeline()
        for corr_id in pending:
            pipe.exists(f"processing:{corr_id}")
        lock_exists = await pipe.execute()
        
        # Collect payments without locks
        for i, corr_id in enumerate(pending):
            if not lock_exists[i]:
                orphaned.append(corr_id)
                
        if orphaned:
            logger.warning(f"Found {len(orphaned)} orphaned payments: {orphaned[:5]}...")
            # Remove these from pending set
            await update_stats_and_release_locks(redis, [], [], orphaned)
    except Exception as e:
        logger.error(f"Error checking orphaned payments: {e}", exc_info=True)

async def connect_redis() -> Optional[aioredis.Redis]:
    """Establish Redis connection with proper retry logic."""
    for attempt in range(5):
        try:
            client = await aioredis.from_url(
                REDIS_URL, 
                decode_responses=True, 
                max_connections=10, 
                health_check_interval=30.0,
                retry_on_timeout=True,
                socket_keepalive=True
            )
            await client.ping()
            logger.info(f"Connected to Redis (attempt {attempt+1}): {REDIS_URL}")
            return client
        except Exception as e:
            logger.warning(f"Redis connection failed (attempt {attempt+1}): {e}")
            if attempt == 4:
                logger.error("Failed to connect to Redis after multiple attempts", exc_info=True)
                return None
            await asyncio.sleep(1)

async def update_worker_status(redis: aioredis.Redis):
    """Update worker status in Redis for monitoring."""
    try:
        status_key = f"worker:{WORKER_ID}:status"
        status_data = {
            "id": WORKER_ID,
            "uptime": time.time() - health_info["start_time"],
            "processed": health_info["processed_count"],
            "success": health_info["success_count"],
            "failures": health_info["failure_count"],
            "last_activity": health_info["last_activity"],
            "status": health_info["status"],
            "timestamp": time.time()
        }
        
        await redis.set(status_key, json.dumps(status_data), ex=60)  # Expire after 60s if worker dies
    except Exception as e:
        logger.error(f"Failed to update worker status: {e}")        

async def main():
    global health_info, redis_client

    logger.info(f"Worker {WORKER_ID} starting...")
    health_info["status"] = "starting"
    cleanup_timer = 0

    while True:
        try:
            redis_client = await connect_redis()
            if not redis_client:
                logger.error("Failed to connect to Redis. Retrying in 5 seconds...")
                await asyncio.sleep(5)
                continue

            health_info["status"] = "running"

            conn = aiohttp.TCPConnector(
                    limit=100, 
                    limit_per_host=20, 
                    enable_cleanup_closed=True,
                    ttl_dns_cache=300
            )

            async with aiohttp.ClientSession(connector=conn) as session:
                while True:
                    try:
                         # Update worker status
                        await update_worker_status(redis_client)

                        current_time = time.time()
                        if current_time - cleanup_timer > 30:
                            # Hook para futuras limpezas de chaves órfãs, se necessário
                            await check_orphaned_payments(redis_client)
                            cleanup_timer = current_time

                        batch = await fetch_batch(redis_client, BATCH_SIZE)
                        if not batch:
                            await asyncio.sleep(0.01)
                            continue

                        await process_batch(session, redis_client, batch)
                    except Exception as e:
                        logger.error(f"Error processing batch: {e}", exc_info=True)
                        health_info["status"] = "error"
                        await asyncio.sleep(1)

        except aioredis.ConnectionError as e:
            logger.error(f"Redis connection error: {e}. Reconnecting...", exc_info=True)
            health_info["status"] = "reconnecting"
            if redis_client:
                await redis_client.close()
                redis_client = None
            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Unexpected error: {e}. Restarting worker...", exc_info=True)
            health_info["status"] = "restarting"
            if redis_client:
                await redis_client.close()
                redis_client = None
            await asyncio.sleep(3)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker shutdown requested")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
