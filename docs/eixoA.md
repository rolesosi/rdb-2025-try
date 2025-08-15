# 📌 Eixo Crítico A — Redis

Este documento descreve como validar se o Redis **não é a causa do problema** no fluxo entre APIs e Worker.

---

## 1. Verificação de Saúde

- Testar se o Redis está respondendo:
  ```bash
  docker exec -it rdb-2025-redis redis-cli ping
retorno esperado: PONG
 
- Testar conextividade das APIs e Worker:
  ```bash
  docker exec -it rdb-2025-api1 redis-cli -h redis ping
  docker exec -it rdb-2025-api2 redis-cli -h redis ping
  docker exec -it rdb-2025-worker redis-cli -h redis ping

## 2.Monitoramento de Latência

- Monitorar os comandos recebidos pelo Redis:
  ```bash
  docker exec -it rdb-2025-redis redis-cli monitor

  📌 Indícios de problema:

  - Explosões de comandos seguidos de longos períodos sem nada.
  - Muitos MGET/SET repetidos e demorados.

## 3.Validação de Consistência
- Testar escrita em API1:
  ```bash
  docker exec -it rdb-2025-api1 redis-cli set testkey "from-api1"

- Validar leitura em API2 e Worker:
  ```bash
  docker exec -it rdb-2025-api2 redis-cli get testkey
  docker exec -it rdb-2025-worker redis-cli get testkey

  📌 Se todas as instâncias enxergarem o valor from-api1, não há falha de consistência.  

## 4. Monitoramento de Recursos
O Redis no docker-compose está configurado com:

- CPU: 0.12
- Memória: 80MB
- Checar consumo em tempo real:
  ```bash
  docker stats rdb-2025-redis

📌 Pontos de atenção:

- CPU próxima de 100% da cota indica gargalo.
- Memória próxima de 80MB pode causar descarte de chaves (maxmemory policy).

## 5. Verificação de Conflitos (Race Conditions)
- Monitorar gravações em chaves:
  ```bash
  docker exec -it rdb-2025-redis redis-cli monitor | grep "SET"

📌 Se múltiplas APIs gravarem na mesma chave em paralelo (ex: order:123), pode haver sobrescrita de dados.

## ✅ Conclusão
Se os testes confirmarem que:
- O Redis responde com PONG.
- O consumo de CPU e Memória está dentro do esperado.
- Todas as instâncias compartilham dados corretamente.
- Não há sinais de sobrescrita de chaves em paralelo.

## ➡️ Então o Redis não é a origem do problema.  