# Monitoreo de MySQL en producción: salud, consultas lentas y atribución de transferencia

Kit para instalar el monitoreo de un servidor MySQL 8 (por ejemplo, una réplica de analítica) en AWS. Contiene solo lo que se despliega en producción: archivos de configuración listos para copiar y una guía paso a paso.

> Todos los nombres, IPs e identificadores de este repositorio son ficticios. Reemplace los marcadores `<...>` con los valores de su entorno.

## Qué mide

| Qué | Cómo | Dónde se ve |
|---|---|---|
| Salud del servidor (CPU, memoria, disco) | `node_exporter` | Grafana (tablero comunitario 1860) |
| Salud de MySQL y atraso de replicación | `mysqld_exporter` | Grafana (tablero 14057 y *DW replica - Data loads*) |
| Consultas de 1 s o más (texto completo) | Grafana Alloy lee el slow log y envía a Loki | Grafana, datasource Loki |
| Bytes enviados por esquema, usuario e IP de cliente | `slowlog-exporter` (código propio, en este repositorio) | *MySQL Data Transfer Attribution* y *Monthly Data Transfer Comparison* |

## Arquitectura

```
 SERVIDOR MYSQL (<DB_PRIVATE_IP>)                  SERVIDOR DE MONITOREO (<MONITORING_PRIVATE_IP>)
 ┌─────────────────────────────────┐              ┌────────────────────────────────┐
 │ MySQL 8 + slow log              │              │ Docker Compose (/opt/monitoring)│
 │ node_exporter     :9100  ◄──────┼──── lee ─────┤ Prometheus :9090 (solo local)  │
 │ mysqld_exporter   :9104  ◄──────┼──── lee ─────┤                                │
 │ slowlog-exporter  :9105  ◄──────┼──── lee ─────┤                                │
 │ Alloy (sentencias ≥ 1 s) ───────┼─── empuja ──►│ Loki :3100                     │
 └─────────────────────────────────┘              │ Grafana :3000 (solo local)     │
                                                  └────────────────────────────────┘
   PC ──SSH──► bastion <BASTION_PUBLIC_IP> ──túnel──► Grafana
```

- En el servidor MySQL solo se instalan agentes livianos como servicios de systemd (sin Docker).
- Prometheus y Grafana no se exponen a la red: Grafana se abre por túnel SSH.
- **La base de datos no se reinicia ni se modifican datos.** El único cambio en MySQL son dos variables de registro (`log_slow_extra` y `long_query_time`), reversibles.

## Estructura

```
.
├── INSTALACION.md                  Guía paso a paso (empiece aquí)
├── monitoring-server/              Se copia a /opt/monitoring en el servidor de monitoreo
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── prometheus/prometheus.yml   (marcador <DB_PRIVATE_IP>)
│   ├── prometheus/rules/egress-rollups.yml
│   ├── loki/loki-config.yml
│   └── grafana/
│       ├── dashboards/             data-transfer, monthly-comparison, data-loads
│       └── provisioning/           datasources y proveedor de tableros
└── db-server/                      Se instala en el servidor MySQL
    ├── mysql/zz-monitoring.cnf
    ├── node_exporter/node_exporter.service
    ├── mysqld_exporter/mysqld_exporter.service
    ├── slowlog-exporter/           código Python, unidad systemd y pruebas
    └── alloy/config.alloy          (marcador <MONITORING_PRIVATE_IP>) + limits.conf
```

## Pruebas del slowlog-exporter

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r db-server\slowlog-exporter\requirements.txt pytest
.venv\Scripts\python -m pytest -q db-server\slowlog-exporter\tests
```

## Siguiente paso

Lea [INSTALACION.md](INSTALACION.md).
