import asyncio, aiohttp, json
import sys
from redis.exceptions import ConnectionError
import redis.asyncio as aioredis
import os

DEFAULT_PROCESSOR_URL = os.getenv("DEFAULT_PROCESSOR_URL")
FALLBACK_PROCESSOR_URL = os.getenv("FALLBACK_PROCESSOR_URL")

if not DEFAULT_PROCESSOR_URL:
    print("Erro: A variável de ambiente DEFAULT_PROCESSOR_URL deve estar definida.")
    sys.exit(1)
if not FALLBACK_PROCESSOR_URL:    
    print("Erro: A variável de ambiente FALLBACK_PROCESSOR_URL  deve estar definida.")
    sys.exit(1)
    
# Usamos um valor um pouco menor para garantir que nosso worker desista antes do cliente.
CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=4)

async def process():
    # A melhor estratégia é usar um timeout no próprio BLPOP para evitar conexões inativas.
    redis_url = os.getenv("REDIS_URL", "redis://redis")
    redis = await aioredis.from_url(redis_url, decode_responses=True)
    async with aiohttp.ClientSession() as session:
        print("Worker iniciado. Aguardando pagamentos...")
        while True:
            try:
                # 1. Tenta obter uma tarefa do Redis.
                # BLPOP com timeout evita que a conexão fique inativa e seja fechada.
                task_tuple = await redis.blpop("payment_queue", timeout=5)
            except ConnectionError as e:
                print("Conexão com o Redis perdida. Tentando reconectar...")
                await asyncio.sleep(1)
                continue # Volta para o início do loop e tenta se reconectar ao Redis

            # Se não houver tarefa, o timeout do blpop foi atingido. Continue o loop.
            if not task_tuple:
                continue

            # 2. Se uma tarefa foi obtida, processe-a.
            _, task = task_tuple
            correlation_id, amount_str = task.split('|')
            payment = {"correlationId": correlation_id, "amount": float(amount_str)}
            processed = False

            # 2.1 Tenta o processador padrão
            try:
                async with session.post(f"{DEFAULT_PROCESSOR_URL}/payments", json=payment, timeout=CLIENT_TIMEOUT) as resp:
                    if resp.status == 200:
                        pipe = redis.pipeline()
                        pipe.hincrby("summary", "success", 1)
                        pipe.hincrbyfloat("summary", "success_amount", float(payment["amount"]))
                        await pipe.execute()
                        processed = True
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass # Falha é esperada, apenas continue para tentar o fallback

            if processed:
                continue

            # 2.2 Se o padrão falhar, tenta o fallback
            try:
                async with session.post(f"{FALLBACK_PROCESSOR_URL}/payments", json=payment, timeout=CLIENT_TIMEOUT) as resp:
                    if resp.status == 200:
                        pipe = redis.pipeline()
                        pipe.hincrby("summary", "fallback", 1)
                        pipe.hincrbyfloat("summary", "fallback_amount", float(payment["amount"]))
                        await pipe.execute()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                # Ambos falharam, o pagamento é perdido. Pode-se adicionar um log aqui.
                pass

asyncio.run(process())