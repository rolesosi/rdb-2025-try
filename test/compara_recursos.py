import yaml
import json

# Caminhos dos arquivos
docker_compose_path = "../docker-compose.yaml"
k6_results_path = "partial-results.json"  # ou o caminho gerado pelo rinha.js

# Lê recursos do docker-compose
with open(docker_compose_path) as f:
    compose = yaml.safe_load(f)

resources = {}
total_cpus = 0.0
total_memory_mb = 0

for name, svc in compose['services'].items():
    limits = svc.get('deploy', {}).get('resources', {}).get('limits', {})
    if limits:
        cpus = float(limits.get('cpus', '0'))
        mem = limits.get('memory', '0MB')
        resources[name] = {
            'cpus': cpus,
            'memory': mem
        }
        total_cpus += cpus
        total_memory_mb += float(mem[:-2])

# Lê resultados do K6
with open(k6_results_path) as f:
    k6 = json.load(f)

# Exibe relatório
print("Recursos definidos no docker-compose:")
for name, res in resources.items():
    print(f"  {name}: CPUs={res['cpus']}, Memória={res['memory']}")
print(f"  TOTAL: CPUs={total_cpus}, Memória={total_memory_mb}MB")

print("\nResultados do K6 (rinha.js):")
for key, value in k6.items():
    if isinstance(value, (int, float, str)):
        print(f"  {key}: {value}")