# 📌 Eixo Crítico B — APIs (api1 e api2)

Este documento descreve como validar se as **APIs** não são a causa do problema no fluxo com Redis e Worker.

---

## 1. Verificação de Saúde

- Testar se cada instância está **rodando** e responde na porta HTTP:

  ```bash
  curl -f http://localhost:8081/health || echo "api1 indisponível"
  curl -f http://localhost:8082/health || echo "api2 indisponível"

- Dentro do container:

  ```bash
  docker exec -it rdb-2025-api1 curl -f http://localhost:8080/health
  docker exec -it rdb-2025-api2 curl -f http://localhost:8080/health

📌 Retorno esperado:

  ```json
  {"status":"ok"}

## 2. Validação de Conectividade com Redis
- Testar se cada API consegue escrever/ler do Redis:

  ```bash
  docker exec -it rdb-2025-api1 redis-cli -h redis set api1_test "ok-api1"
  docker exec -it rdb-2025-api1 redis-cli -h redis get api1_test

  docker exec -it rdb-2025-api2 redis-cli -h redis set api2_test "ok-api2"
  docker exec -it rdb-2025-api2 redis-cli -h redis get api2_test
📌 Se ambos conseguem escrever/ler, não há falha de integração Redis <-> API.

## 3. Validação de Consistência entre Instâncias
- Escrever pela api1 e ler pela api2:

  ```bash
  docker exec -it rdb-2025-api1 redis-cli -h redis set shared_key "from-api1"
  docker exec -it rdb-2025-api2 redis-cli -h redis get shared_key
- Escrever pela api2 e ler pela api1:

  ```bash
  docker exec -it rdb-2025-api2 redis-cli -h redis set shared_key "from-api2"
  docker exec -it rdb-2025-api1 redis-cli -h redis get shared_key
📌 Se os valores são lidos corretamente, não há problema de sincronização.

## 4. Monitoramento de Recursos
As APIs no docker-compose estão configuradas com:

- CPU: 0.25 cada
- Memória: 256MB cada
- Checar consumo em tempo real:

  ```bash
  docker stats rdb-2025-api1 rdb-2025-api2
📌 Pontos de atenção:

- Se CPU atingir 100% da cota → gargalo de processamento.
- Se memória atingir limite de 256MB → pode causar reinício ou OOMKilled.

## 5. Logs de Erro e Timeout
- Verificar logs da aplicação:

  ```bash
  docker logs rdb-2025-api1 --tail=100 -f
  docker logs rdb-2025-api2 --tail=100 -f
📌 Indícios de problema:

- Mensagens de timeout no Redis.
- Erros 5xx recorrentes.
- Falhas em processamento de requisições simultâneas.

## 6. Testes de Carga e Conexões
- Simular múltiplas requisições:

  ```bash
  ab -n 1000 -c 50 http://localhost:8081/api/test
  ab -n 1000 -c 50 http://localhost:8082/api/test
📌 Se apenas uma instância apresenta lentidão ou falhas → problema isolado.
Se ambas degradam ao mesmo tempo → investigar dependências (Redis, Worker, Nginx).

## ✅ Conclusão
Se os testes confirmarem que:
- Ambas as APIs respondem ao /health.
- Conectividade Redis funciona em ambas direções.
- Não há falhas de memória/CPU em docker stats.
- Logs não mostram erros críticos.
- Testes de carga mantêm respostas consistentes.

## ➡️ Então as APIs não são a origem do problema.