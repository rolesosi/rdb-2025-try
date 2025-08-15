# 📌 Eixo Crítico B — APIs (api1 e api2)

## Contexto
O eixo B refere-se à **camada de persistência** da aplicação, onde `api1` e `api2` interagem diretamente com o **banco de dados** para realizar operações de leitura e escrita.  
Esse elo é crítico porque um problema nessa comunicação pode causar:
- Lentidão generalizada nas requisições.
- Erros de consistência de dados.
- Interrupção de funcionalidades essenciais para o usuário.

---

## Possíveis Fontes de Problema

1. **Conexões Excedidas**
   - Número de conexões simultâneas abertas pelas APIs ultrapassa o limite do banco.
   - Resultado: erros de conexão ou quedas intermitentes.

2. **Consultas Não Otimizadas**
   - Queries pesadas, sem índices adequados ou com planos de execução ineficientes.
   - Resultado: aumento de latência e bloqueio de recursos.

3. **Locking e Deadlocks**
   - Concorrência em transações pode gerar bloqueios e falhas de execução.
   - Resultado: timeouts, rollback de transações e queda no throughput.

4. **Pool de Conexões Mal Configurado**
   - Pools pequenos causam espera para novas conexões.
   - Pools grandes sobrecarregam o banco.
   - Resultado: degradação de performance.

5. **Problemas de Rede**
   - Latência, perdas de pacote ou instabilidade na comunicação entre API e banco.
   - Resultado: falhas intermitentes ou lentidão percebida pelo usuário.

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