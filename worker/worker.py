import asyncio, aiohttp, json
import sys
import aioredis
import os

DEFAULT_PROCESSOR_URL = os.getenv("DEFAULT_PROCESSOR_URL")
FALLBACK_PROCESSOR_URL = os.getenv("FALLBACK_PROCESSOR_URL")

if not DEFAULT_PROCESSOR_URL :
    print("Erro: A variável de ambiente DEFAULT_PROCESSOR_URL deve estar definida.")
    sys.exit(1)
if not FALLBACK_PROCESSOR_URL:    
    print("Erro: A variável de ambiente FALLBACK_PROCESSOR_URL  deve estar definida.")
    sys.exit(1)
    
async def process():
    redis = await aioredis.from_url("redis://redis", decode_responses=True)
    async with aiohttp.ClientSession() as session:
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
                        await redis.hincrbyfloat("summary", "success_amount", float(payment["amount"]))
                        continue
            except:
                pass

            await redis.hincrby("summary", "fallback", 1)
            await redis.hincrbyfloat("summary", "fallback_amount", float(payment["amount"]))

async def choose_processor(redis):
    a_health = await redis.get("procA_health") or "ok"
    if a_health == "ok":
        return DEFAULT_PROCESSOR_URL
    return FALLBACK_PROCESSOR_URL

asyncio.run(process())