# Matriz de Inconsistências – API1/API2 + Worker + Redis

## Contexto
O sistema é composto por:
- 2 instâncias da **API** (`api1`, `api2`), mesma imagem.
- **Redis** como store de dados temporários.
- **Worker** singleton para processamento assíncrono.
- **Nginx** como balanceador.
- Integração com **payment-processor-default** e **payment-processor-fallback**.

---

## Riscos Identificados

| Origem | Efeito Técnico | Impacto no Desafio |
|--------|----------------|--------------------|
| Concorrência no Redis (api1 vs api2) | Escritas simultâneas → race conditions. | Saldos/estados incorretos, respostas diferentes. |
| Operações não atômicas no Redis | Uso de GET+SET em vez de INCR/DECR. | Contadores/saldos divergentes sob carga. |
| Expiração de chaves (TTL) | api1 vê dado, api2 não. | Respostas incoerentes para o mesmo cliente. |
| Nginx balanceando estado local | Cache/sessão não compartilhada. | Sessão perdida ao trocar de instância. |
| Performance desigual entre instâncias | Latência variável entre api1 e api2. | Parte dos clientes tem timeout/lentidão. |
| Worker singleton | Gargalo de fila. | API confirma antes de worker processar. |
| Redis como SPOF com 80MB | Estouro → eviction ou crash. | Perda de dados temporários; falha em testes de carga. |
| Eviction policy do Redis | Chaves descartadas silenciosamente. | Estado “desaparece” entre requisições. |
| Falha entre processador default/fallback | Resposta divergente. | API marca como pago mas worker rejeita. |
| Limites de CPU/memória (1.5 CPU / 350MB) | Estrangulamento ou OOM kill. | Reinício de pods no meio da transação. |
| Configuração divergente | Variáveis de ambiente diferentes por instância. | Comportamentos incoerentes (default vs fallback). |

---

## Eixos Críticos

- **A**: Redis (concorrência, TTL, eviction).  
- **B**: API1/API2 (mesma imagem, diferentes estados).  
- **C**: Worker singleton (ponto único de gargalo).  
- **D**: Infra limitada (CPU/RAM).  
- **E**: Integrações externas (processadores inconsistentes).
