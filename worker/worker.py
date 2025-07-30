import asyncio, aiohttp, json
import aioredis

async def process():
    redis = await aioredis.from_url("redis://redis", decode_responses=True)
    session = aiohttp.ClientSession()

    while True:
        task = await redis.lpop("payment_queue")
        if not task:
            await asyncio.sleep(0.01)
            continue

        payment = json.loads(task)
        processor_url = await choose_processor(redis)

        try:
            async with session.post(f"{processor_url}/payments", json=payment, timeout=0.005) as resp:
                if resp.status == 200:
                    await redis.hincrby("summary", "success", 1)
                    continue
        except:
            pass

        await redis.hincrby("summary", "fallback", 1)

async def choose_processor(redis):
    a_health = await redis.get("procA_health") or "ok"
    if a_health == "ok":
        return "http://payment-processor-a"
    return "http://payment-processor-b"

asyncio.run(process())