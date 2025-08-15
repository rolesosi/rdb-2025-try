# 📌 Análise do Eixo Crítico C: Comunicação entre Worker e Payment Processors

## Contexto
O eixo C trata da **integração do worker** com os **Payment Processors**, que processam os pagamentos.  
Problemas neste eixo podem gerar:

- Falhas na entrega de pagamentos.
- Diferenças entre contadores `default` e `fallback`.
- Inconsistência nos dados observados pelo endpoint `/payments-summary`.

O worker é responsável por consumir tarefas do Redis e enviar requisições HTTP para os processors padrão e fallback.

---

## Possíveis Fontes de Problema

1. **Timeouts de HTTP**
   - O worker possui timeout de 1,4s para o client.
   - Requests longas ou lentas podem ser cortadas antes de concluir.
   
2. **Falhas de Conexão**
   - `ConnectionResetError` ou indisponibilidade temporária do processor.
   - Resultado: pagamentos perdidos ou contagem inconsistente.

3. **Concorrência do Worker**
   - Apenas uma instância do worker processando tarefas pode gerar fila acumulada no Redis.
   - Resultado: latência e inconsistência nos resumos.

4. **Formato ou Dados Inválidos**
   - `correlationId` deve ser UUID e `amount` decimal.
   - Requests inválidas podem ser rejeitadas silenciosamente.

5. **Problemas de Rede**
   - Latência ou perda de pacotes entre container worker e processors.
   - Resultado: falhas intermitentes.

---

## 1. Testar endpoints manualmente
- Envia um pagamento para o processor default
  ```bash
  curl -X POST http://payment-processor-default:8080/payments \
    -H "Content-Type: application/json" \
    -d '{"correlationId":"11111111-1111-1111-1111-111111111111","amount":19.90}'

- Envia um pagamento para o processor fallback
  ```bash
  curl -X POST http://payment-processor-fallback:8080/payments \
    -H "Content-Type: application/json" \
    -d '{"correlationId":"22222222-2222-2222-2222-222222222222","amount":9.90}'

## 2. Validar resumos
- Consultar resumo dos pagamentos processados
  ```bash
  curl -X GET "http://localhost:9999/payments-summary?from=2025-08-01T00:00:00Z&to=2025-08-15T23:59:59Z"

## 3. Monitorar o worker
- Visualizar logs em tempo real do container
  ```bash
  docker logs -f rdb-2025-worker

- Executar shell no container para testes
  ```bash
  docker exec -it rdb-2025-worker /bin/sh

## 4. Testes de stress
- Simular múltiplos pagamentos para verificar concorrência e timeout
  ```bash
  for i in {1..50}; do
    curl -X POST http://payment-processor-default:8080/payments \
      -H "Content-Type: application/json" \
      -d "{\"correlationId\":\"$(uuidgen)\",\"amount\":10.0}" &
  done
  wait

## Métricas a Serem Monitoradas
- Taxa de sucesso/falha de requisições HTTP do worker.
- Tempo de resposta dos processors.
- Tamanho da fila no Redis.
- Verificar tamanho da fila payment_queue
  ```bash
  docker exec -it rdb-2025-redis redis-cli LLEN payment_queue
- Inconsistência entre default e fallback no resumo /payments-summary.

##Conclusão
O eixo C é crítico para a consistência final dos pagamentos.
Validar este eixo garante que:
- O worker consegue processar a fila sem perder pagamentos.
- Os Payment Processors respondem dentro do tempo esperado.
- O resumo consolidado (/payments-summary) reflete corretamente todas as operações.