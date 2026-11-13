# Instalación en producción, paso a paso

Esta guía instala el monitoreo de un servidor MySQL 8 en AWS. Cada paso indica **dónde** se ejecuta, **qué** hace y **cómo** se verifica. Ejecute los comandos de uno en uno y verifique antes de seguir.

## Tabla de contenido

0. [Antes de empezar](#0-antes-de-empezar)
1. [Relevamiento de red](#paso-1-relevamiento-de-red-consola-solo-lectura)
2. [Grupo de seguridad e instancia de monitoreo](#paso-2-grupo-de-seguridad-e-instancia-de-monitoreo-consola)
3. [Docker](#paso-3-docker-monitoreo)
4. [Stack de monitoreo](#paso-4-stack-de-monitoreo-monitoreo)
5. [Relevamiento del servidor MySQL](#paso-5-relevamiento-del-servidor-mysql-solo-lectura)
6. [node_exporter](#paso-6-node_exporter-servidor-mysql)
7. [mysqld_exporter](#paso-7-usuario-de-métricas-y-mysqld_exporter-servidor-mysql)
8. [Abrir los exporters al monitoreo](#paso-8-abrir-los-exporters-al-monitoreo-consola)
9. [Registro completo en MySQL](#paso-9-activar-el-registro-completo-en-mysql-servidor-mysql)
10. [slowlog-exporter](#paso-10-slowlog-exporter-pc-y-servidor-mysql)
11. [Alloy](#paso-11-alloy-sentencias-de-1-s-o-más-a-loki-servidor-mysql)
12. [Reglas y tableros](#paso-12-reglas-y-tableros-pc-y-monitoreo)
13. [Verificación rápida](#13-verificación-rápida)
14. [Cómo revertir](#14-cómo-revertir)
15. [Lecciones aprendidas](#15-lecciones-aprendidas)
16. [Mejoras pendientes](#16-mejoras-pendientes)

---

## 0. Antes de empezar

### Valores de su entorno

Anote estos valores; los marcadores aparecen en los comandos y en dos archivos del kit.

| Marcador | Qué es | Ejemplo ficticio |
|---|---|---|
| `<DB_PRIVATE_IP>` | IP privada del servidor MySQL | `10.20.64.4` |
| `<MONITORING_PRIVATE_IP>` | IP privada del servidor de monitoreo (se conoce en el paso 2) | `10.20.76.35` |
| `<BASTION_PUBLIC_IP>` | IP pública del bastion SSH | `203.0.113.10` |
| `<BASTION_PRIVATE_IP>` | IP privada del bastion | `10.20.23.87` |
| `<VPC_ID>` / `<SUBNET_ID>` | VPC y subred privada del servidor MySQL | `vpc-0123456789abcdef0` |
| `<KEY_FILE>` | Llave SSH (`.pem`) | `my-key.pem` |
| `<DB_SERVER_NAME>` | Nombre de la instancia MySQL en EC2 | `db-replica-01` |

Archivos con marcadores:

- `monitoring-server/prometheus/prometheus.yml` → `<DB_PRIVATE_IP>`
- `db-server/alloy/config.alloy` → `<MONITORING_PRIVATE_IP>`

> **Etiqueta `instance`.** Los tableros y la configuración usan `instance: db-replica-01`. Si usa otro nombre, cámbielo en `prometheus.yml`, en `config.alloy` y en `grafana/dashboards/data-loads.json` (buscar y reemplazar).

### Requisitos

- Servidor MySQL 8 en Ubuntu 22.04 o 24.04, con `performance_schema = ON`.
- Una subred privada con salida a internet (NAT) para descargar paquetes.
- Un bastion SSH con acceso a la subred privada.
- Aprobación del equipo para activar `long_query_time = 0` (paso 9). Mida antes el volumen esperado del slow log (paso 5).

### Acceso SSH desde Windows (PowerShell)

Ejecute siempre desde la carpeta donde está la llave; si no, SSH responde `Identity file ... not accessible`.

```powershell
# Servidor MySQL
ssh -i <KEY_FILE> -o "ProxyCommand=ssh -i <KEY_FILE> -W %h:%p ubuntu@<BASTION_PUBLIC_IP>" ubuntu@<DB_PRIVATE_IP>

# Servidor de monitoreo
ssh -i <KEY_FILE> -o "ProxyCommand=ssh -i <KEY_FILE> -W %h:%p ubuntu@<BASTION_PUBLIC_IP>" ubuntu@<MONITORING_PRIVATE_IP>

# Grafana por túnel: deje la ventana abierta y abra http://localhost:3000
ssh -i <KEY_FILE> -o "ProxyCommand=ssh -i <KEY_FILE> -W %h:%p ubuntu@<BASTION_PUBLIC_IP>" -L 3000:localhost:3000 -N ubuntu@<MONITORING_PRIVATE_IP>
```

> **Antes de pegar un comando, mire el prompt** (`ubuntu@ip-10-20-64-4` frente a `ubuntu@ip-10-20-76-35`). Instalar algo en el servidor equivocado es el error más común.

---

### Paso 1. Relevamiento de red (consola, solo lectura)

En la consola de EC2 anote, del servidor MySQL: IP privada, VPC, subred y grupos de seguridad; y la IP privada del bastion. Revise también qué redes tienen acceso a los puertos del servidor MySQL.

### Paso 2. Grupo de seguridad e instancia de monitoreo (consola)

1. **Grupo de seguridad `monitoring-prod-sg`** en `<VPC_ID>`:
   - SSH 22 desde `<BASTION_PRIVATE_IP>/32`.
   - TCP 3100 desde `<DB_PRIVATE_IP>/32` (Alloy envía registros a Loki).
2. **Instancia `monitoring-prod`:** Ubuntu Server 24.04 LTS x86_64, `t3.medium`, subred `<SUBNET_ID>`, **sin IP pública**, SG `monitoring-prod-sg`, disco de 50 GiB gp3, metadatos **solo IMDSv2**.
3. Anote su IP privada: es `<MONITORING_PRIVATE_IP>`.

> El nombre de un SG no puede empezar con `sg-` (AWS lo reserva para los ID). Escriba las IP a mano: al pegarlas, un carácter invisible puede producir `CIDR block ... is malformed`. En el asistente de EC2, elija la AMI **antes** de configurar discos y reglas: cambiarla después los borra.

**Verificación:** por SSH, `curl -sI https://download.docker.com | head -1` responde `HTTP/2 200`.

### Paso 3. Docker (monitoreo)

Se instala desde el repositorio oficial de Docker; el de Ubuntu suele estar atrasado.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker ubuntu
```

Cierre y abra la sesión SSH para que `usermod` tenga efecto.

**Verificación:** `docker --version; docker compose version; docker run --rm hello-world` imprime `Hello from Docker!`.

### Paso 4. Stack de monitoreo (monitoreo)

1. **En el PC**, reemplace `<DB_PRIVATE_IP>` en `monitoring-server/prometheus/prometheus.yml`.
2. **Copie la carpeta** (PowerShell, en la carpeta de la llave):

```powershell
$kit = "C:\ruta\al\kit\monitoring-server"
scp -r -i <KEY_FILE> -o "ProxyCommand=ssh -i <KEY_FILE> -W %h:%p ubuntu@<BASTION_PUBLIC_IP>" "$kit" ubuntu@<MONITORING_PRIVATE_IP>:/tmp/monitoring
```

3. **En el servidor de monitoreo:**

```bash
sudo mkdir -p /opt/monitoring
sudo cp -r /tmp/monitoring/. /opt/monitoring/
sudo chown -R ubuntu:ubuntu /opt/monitoring
rm -rf /tmp/monitoring
cd /opt/monitoring

# Permisos legibles para los usuarios de los contenedores (scp desde Windows puede dejarlos cerrados)
find . -type d -exec chmod 755 {} +
find . -type f -exec chmod 644 {} +

# Contraseña inicial de Grafana
cp .env.example .env
sed -i "s|^GF_SECURITY_ADMIN_PASSWORD=.*|GF_SECURITY_ADMIN_PASSWORD=$(openssl rand -base64 18)|" .env
chmod 600 .env
grep GF_SECURITY_ADMIN_PASSWORD .env   # guárdela en el gestor de contraseñas del equipo
```

4. **Validar y arrancar:**

```bash
cd /opt/monitoring
docker run --rm --entrypoint promtool -v ./prometheus:/etc/prometheus:ro prom/prometheus:v3.1.0 check config /etc/prometheus/prometheus.yml
docker compose config -q && echo "compose OK"
docker compose up -d
docker compose ps
curl -s localhost:3100/ready; echo
```

**Verificación:** `promtool` responde `SUCCESS` para la configuración **y para las reglas** (si no menciona `egress-rollups.yml`, las reglas no se cargaron); los tres contenedores en `Up`; Loki responde `ready`.

| Decisión | Para qué |
|---|---|
| Versiones fijas, nunca `latest` | Ninguna actualización llega sin decidirla. |
| Retención de Prometheus: 90 días o 15 GB | El disco de 50 GB nunca se llena. |
| Retención de Loki: 30 días (`retention_enabled: true` en el compactor) | Sin esa opción, Loki no borra nada. |
| `mem_limit` por contenedor | Si uno se descontrola, no afecta a los demás. |
| `restart: unless-stopped` | Los contenedores vuelven solos tras un reinicio. |
| UID `prometheus` y `loki` en los datasources | Coinciden con los que usan los tableros. |

**Cambiar la contraseña de Grafana más adelante** (Grafana solo lee el `.env` en el primer arranque):

```bash
cd /opt/monitoring
read -rs -p "Nueva contraseña: " P; echo
docker compose exec grafana grafana cli admin reset-admin-password "$P"
unset P
sed -i '/GF_SECURITY_ADMIN_PASSWORD/d' .env
```

### Paso 5. Relevamiento del servidor MySQL (solo lectura)

```bash
date; timedatectl | grep -E "Local time|synchronized|NTP"
cat /etc/os-release | head -2; nproc; free -h; df -h
mysql --version
sudo ss -ltnp | grep -E ':(9100|9104|9105|3306|12345)\b'
sudo mysql -e "SHOW GLOBAL STATUS WHERE Variable_name IN ('Questions','Uptime','Threads_connected','Slow_queries'); SHOW VARIABLES WHERE Variable_name IN ('slow_query_log','slow_query_log_file','long_query_time','log_output','datadir','read_only','log_slow_replica_statements','performance_schema');"
sudo mysql -e "SHOW REPLICA STATUS\G" | grep -E "Channel_Name|Source_Host|Replica_IO_Running|Replica_SQL_Running:|Seconds_Behind_Source"
sudo ls -lh /var/log/mysql/
cat /etc/logrotate.d/mysql-server
```

| Revise | Por qué |
|---|---|
| Reloj sincronizado por NTP | Las métricas y los registros se comparan por hora. |
| Espacio libre en disco | El slow log va a crecer en el paso 9. |
| `Questions / Uptime` | Sentencias por segundo. Con `long_query_time = 0`, cada sentencia ocupa ≈ 1 KB en el slow log: 1 sentencia/s ≈ 86 MB por día. |
| `slow_query_log_file` | Ruta que leen el slowlog-exporter y Alloy (por defecto `/var/log/mysql/mysql-slow.log`). Si es otra, ajústela en la unidad systemd y en `config.alloy`. |
| Rotación en `/etc/logrotate.d/mysql-server` | Debe incluir el slow log. Si no, agregue una regla propia (diaria, comprimida). |
| Puertos 9100, 9104, 9105 y 12345 libres | Los usan los agentes. |
| `performance_schema = ON` | Necesario para el panel *Slowest queries*. |

### Paso 6. node_exporter (servidor MySQL)

Publica métricas del sistema operativo en el puerto 9100.

**Copiar desde el PC** los archivos de los pasos 6, 7 y 9 (PowerShell, en la carpeta de la llave):

```powershell
$src = "C:\ruta\al\kit\db-server"
scp -i <KEY_FILE> -o "ProxyCommand=ssh -i <KEY_FILE> -W %h:%p ubuntu@<BASTION_PUBLIC_IP>" "$src\node_exporter\node_exporter.service" "$src\mysqld_exporter\mysqld_exporter.service" "$src\mysql\zz-monitoring.cnf" ubuntu@<DB_PRIVATE_IP>:/tmp/
```

**En el servidor MySQL:**

```bash
cd /tmp
curl -fsSLO https://github.com/prometheus/node_exporter/releases/download/v1.8.2/node_exporter-1.8.2.linux-amd64.tar.gz
curl -fsSLO https://github.com/prometheus/node_exporter/releases/download/v1.8.2/sha256sums.txt
grep " node_exporter-1.8.2.linux-amd64.tar.gz$" sha256sums.txt | sha256sum -c -
tar xzf node_exporter-1.8.2.linux-amd64.tar.gz
sudo install -m 0755 node_exporter-1.8.2.linux-amd64/node_exporter /usr/local/bin/
sudo useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter
```

```bash
sudo install -m 0644 /tmp/node_exporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now node_exporter
```

| Elemento | Para qué |
|---|---|
| `sha256sum -c` | Comprueba que el binario descargado es el publicado. Debe decir `OK`. |
| Usuario de sistema sin shell | Si el programa fallara, el daño queda contenido. |
| `MemoryMax`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome` | Tope de memoria y blindaje del servicio. |

**Verificación:** `curl -s localhost:9100/metrics | grep "^node_load1 "`.

### Paso 7. Usuario de métricas y mysqld_exporter (servidor MySQL)

Publica métricas de MySQL y de la replicación en el puerto 9104.

```bash
cd /tmp
curl -fsSLO https://github.com/prometheus/mysqld_exporter/releases/download/v0.15.1/mysqld_exporter-0.15.1.linux-amd64.tar.gz
curl -fsSLO https://github.com/prometheus/mysqld_exporter/releases/download/v0.15.1/sha256sums.txt
grep " mysqld_exporter-0.15.1.linux-amd64.tar.gz$" sha256sums.txt | sha256sum -c -
tar xzf mysqld_exporter-0.15.1.linux-amd64.tar.gz
sudo install -m 0755 mysqld_exporter-0.15.1.linux-amd64/mysqld_exporter /usr/local/bin/
sudo useradd --system --no-create-home --shell /usr/sbin/nologin mysqld_exporter

P=$(openssl rand -hex 24)
sudo mysql <<SQL
SET sql_log_bin = 0;
CREATE USER 'exporter'@'localhost' IDENTIFIED BY '$P' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'exporter'@'localhost';
GRANT SELECT ON performance_schema.* TO 'exporter'@'localhost';
SET sql_log_bin = 1;
SQL

sudo mkdir -p /etc/mysqld_exporter
sudo tee /etc/mysqld_exporter/.my.cnf > /dev/null <<EOF
[client]
user=exporter
password=$P
socket=/var/run/mysqld/mysqld.sock
EOF
unset P
sudo chown -R mysqld_exporter:mysqld_exporter /etc/mysqld_exporter
sudo chmod 700 /etc/mysqld_exporter
sudo chmod 600 /etc/mysqld_exporter/.my.cnf
```

```bash
sudo install -m 0644 /tmp/mysqld_exporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mysqld_exporter
```

| Elemento | Para qué |
|---|---|
| `SET sql_log_bin = 0` | La creación del usuario no se escribe en el binlog. Evita una transacción GTID ajena si el servidor fuera origen de replicación. |
| `'exporter'@'localhost'` | Solo se conecta desde la propia máquina. |
| `MAX_USER_CONNECTIONS 3` | Nunca puede saturar MySQL. |
| `PROCESS`, `REPLICATION CLIENT`, `SELECT ON performance_schema.*` | Solo lee estadísticas. **No ve tablas de negocio.** |
| Contraseña generada en una variable | Nunca se muestra ni queda en el historial; solo existe en `.my.cnf` (600). |
| `--collect.perf_schema.eventsstatements` | Sentencias normalizadas (sin valores literales) para el panel *Slowest queries*. |

**Verificación:** `curl -s localhost:9104/metrics | grep -E "^mysql_up |seconds_behind_master"` muestra `mysql_up 1` y una línea por canal de replicación.

### Paso 8. Abrir los exporters al monitoreo (consola)

1. Cree el SG `db-metrics-sg` en `<VPC_ID>`, con entradas TCP 9100, 9104 y 9105 **desde el SG `monitoring-prod-sg`** (no desde una IP).
2. EC2 → `<DB_SERVER_NAME>` → **Acciones → Seguridad → Cambiar grupos de seguridad** → **agregar** `db-metrics-sg`. **No quite** los SG que ya tiene.

Agregar un SG propio, en vez de editar uno existente, solo suma permisos, no afecta a otras instancias que compartan ese SG y se revierte en un clic.

**Verificación (monitoreo):**

```bash
curl -s localhost:9090/api/v1/targets | grep -o '"job":"[a-z]*"\|"health":"[a-z]*"' | uniq
```

Los jobs `prometheus`, `node` y `mysql` en `"health":"up"`. `slowlog` queda en `down` hasta el paso 10.

### Paso 9. Activar el registro completo en MySQL (servidor MySQL)

Necesario para atribuir bytes: sin cada sentencia y su `Bytes_sent`, los totales serían incompletos. **Requiere aprobación previa.**

```bash
sudo mysql -e "SET GLOBAL log_slow_extra = ON; SET GLOBAL long_query_time = 0;"
```

Para que sobreviva a un reinicio de MySQL, instale `zz-monitoring.cnf` (copiado a `/tmp` en el paso 6):

```bash
sudo install -m 0644 /tmp/zz-monitoring.cnf /etc/mysql/mysql.conf.d/zz-monitoring.cnf
rm -f /tmp/zz-monitoring.cnf
my_print_defaults mysqld | grep -E "slow_query_log|log_slow_extra|long_query_time"
```

| Pregunta | Respuesta |
|---|---|
| ¿Reinicia MySQL? | No. Las variables son dinámicas. |
| ¿Modifica datos? | No. Solo cambia qué se escribe en el registro. |
| ¿Afecta la replicación? | No, con `log_slow_replica_statements = OFF`: lo que llega del primario no se registra. |
| ¿Cuándo aplica? | A las conexiones nuevas. |
| ¿Por qué un archivo y no `SET PERSIST`? | `SET PERSIST` guarda en `mysqld-auto.cnf`, dentro del directorio de datos, donde es fácil pasarlo por alto. El prefijo `zz-` hace que se lea al final. |

`my_print_defaults` muestra lo que MySQL leerá en el próximo arranque, sin reiniciar.

**Verificación:** `sudo grep -m1 "Bytes_sent" /var/log/mysql/mysql-slow.log` muestra una línea con `Bytes_sent:`. Mida el crecimiento del archivo durante el primer día.

### Paso 10. slowlog-exporter (PC y servidor MySQL)

Lee el slow log y suma los bytes por esquema, usuario e IP en el puerto 9105.

**Copiar desde el PC** (PowerShell, en la carpeta de la llave):

```powershell
$src = "C:\ruta\al\kit\db-server"
scp -i <KEY_FILE> -o "ProxyCommand=ssh -i <KEY_FILE> -W %h:%p ubuntu@<BASTION_PUBLIC_IP>" "$src\slowlog-exporter\slowlog_exporter.py" "$src\slowlog-exporter\slowlog_parser.py" "$src\slowlog-exporter\requirements.txt" "$src\slowlog-exporter\slowlog-exporter.service" ubuntu@<DB_PRIVATE_IP>:/tmp/
```

Use `scp`; no pegue archivos largos en la terminal (se cortan o se alteran).

**Instalar en el servidor MySQL:**

```bash
sudo NEEDRESTART_MODE=l apt-get install -y --no-install-recommends python3-venv
sudo useradd --system --no-create-home --shell /usr/sbin/nologin slowlog
sudo mkdir -p /opt/slowlog-exporter
sudo install -m 0644 /tmp/slowlog_exporter.py /tmp/slowlog_parser.py /tmp/requirements.txt /opt/slowlog-exporter/
sudo python3 -m venv /opt/slowlog-exporter/venv
sudo /opt/slowlog-exporter/venv/bin/pip install --quiet -r /opt/slowlog-exporter/requirements.txt
sudo install -m 0644 /tmp/slowlog-exporter.service /etc/systemd/system/
rm -f /tmp/slowlog_exporter.py /tmp/slowlog_parser.py /tmp/requirements.txt /tmp/slowlog-exporter.service
sudo systemctl daemon-reload
sudo systemctl enable --now slowlog-exporter
```

| Elemento | Para qué |
|---|---|
| `NEEDRESTART_MODE=l` | Solo lista los servicios con bibliotecas viejas; no pregunta ni reinicia nada. |
| Entorno virtual | Las librerías del exporter no tocan el Python del sistema. |
| `Group=adm` + `SupplementaryGroups=mysql` | Permiso de lectura sobre el slow log (`mysql:adm`, 640). |
| `--state-file` | Guarda la posición de lectura: tras un reinicio continúa sin perder ni duplicar. |
| Usuarios excluidos | `exporter`, `root` y `debian-sys-maint` no cuentan en los totales. |

**Verificación:** `curl -s localhost:9105/metrics | grep "^mysql_slowlog_bytes_sent_total"`. En el monitoreo, el job `slowlog` pasa a `up`.

### Paso 11. Alloy: sentencias de 1 s o más a Loki (servidor MySQL)

1. **En el PC**, reemplace `<MONITORING_PRIVATE_IP>` en `db-server/alloy/config.alloy` y copie `config.alloy` y `limits.conf` a `/tmp/` del servidor MySQL con `scp`.
2. **En el servidor MySQL:**

```bash
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list > /dev/null
sudo apt-get update -o Dir::Etc::sourcelist=sources.list.d/grafana.list -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0
sudo NEEDRESTART_MODE=l apt-get install -y --no-install-recommends alloy
sudo apt-mark hold alloy
sudo usermod -aG adm alloy
sudo mkdir -p /etc/systemd/system/alloy.service.d
sudo install -m 0644 /tmp/limits.conf /etc/systemd/system/alloy.service.d/limits.conf
sudo install -m 0644 /tmp/config.alloy /etc/alloy/config.alloy
rm -f /tmp/limits.conf /tmp/config.alloy

sudo alloy fmt /etc/alloy/config.alloy > /dev/null && echo "config OK"
sudo systemctl daemon-reload
sudo systemctl enable alloy
sudo systemctl restart alloy
```

| Elemento | Para qué |
|---|---|
| `apt-get update` limitado al repositorio de Grafana | No toca las demás actualizaciones pendientes del servidor. |
| `apt-mark hold alloy` | Un `apt upgrade` futuro no lo actualiza sin decidirlo. |
| `alloy fmt` antes de reiniciar | Un error de sintaxis deja a Alloy reiniciándose en bucle. Valide siempre antes. |
| `stage.multiline` | Une las líneas de cada entrada del slow log en un solo registro. |
| `stage.drop` | Descarta las sentencias de menos de 1 s antes de salir del servidor (volumen y valores literales). |
| `tail_from_end = true` | Empieza desde la instalación; no reenvía lo viejo. |

**Verificación (servidor MySQL):** `curl -s localhost:12345/metrics | grep faster_than_1s` muestra un contador que crece. **(Monitoreo):** `curl -s localhost:3100/loki/api/v1/label/job/values` incluye `mysql-slowlog`.

### Paso 12. Reglas y tableros (PC y monitoreo)

Las reglas (`prometheus/rules/egress-rollups.yml`) y los tableros (`grafana/dashboards/*.json`) ya se copiaron en el paso 4. Solo verifique:

```bash
cd /opt/monitoring
docker run --rm --entrypoint promtool -v ./prometheus:/etc/prometheus:ro prom/prometheus:v3.1.0 check rules /etc/prometheus/rules/egress-rollups.yml
curl -s localhost:9090/api/v1/rules | grep -o '"name":"[a-z_]*"' | sort -u
```

`egress-rollups.yml` precalcula cada 5 minutos los totales por esquema y por usuario/IP (sin eso, el tablero mensual leería millones de muestras) e incluye las alertas `AttributionCoverageLow`, `DailyEgressSpike` y `MonthlyEgressBudgetExceeded`. Ajuste el presupuesto de `MonthlyEgressBudgetExceeded` a su realidad.

Para actualizar un tablero después: cópielo con `scp` a `/opt/monitoring/grafana/dashboards/`; Grafana lo recarga solo en unos segundos.

**Tableros en Grafana** (carpeta **Data transfer**):

| Tablero | Qué muestra |
|---|---|
| *MySQL Data Transfer Attribution* | Bytes por esquema, usuario e IP; consultas más lentas. Los dos paneles de CloudWatch quedan vacíos hasta conectar CloudWatch (ver [Mejoras](#16-mejoras-pendientes)). |
| *Monthly Data Transfer Comparison* | Comparación de 30 días. Se completa con el tiempo: necesita 30 días de datos (las reglas no rellenan el pasado). |
| *DW replica - Data loads* | Cuándo llegan cargas y por qué camino: si suben las filas escritas **y** el atraso, es replicación; si suben las filas **y** las sentencias directas, es una carga directa. |

**Tableros comunitarios** (Dashboards → New → Import, datasource `Prometheus`): `1860` (Node Exporter Full) y `14057` (MySQL).

---

## 13. Verificación rápida

**En el monitoreo:**

```bash
cd /opt/monitoring
docker compose ps --format '{{.Service}} {{.Status}}'
curl -s localhost:9090/api/v1/targets | grep -o '"job":"[a-z]*"\|"health":"[a-z]*"' | uniq
curl -s localhost:3100/ready; echo
```

**En el servidor MySQL:**

```bash
systemctl is-active node_exporter mysqld_exporter slowlog-exporter alloy
sudo mysql -N -e "SELECT @@log_slow_extra, @@long_query_time;"
sudo mysql -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running:|Seconds_Behind_Source"
```

- [ ] Los tres contenedores en `Up`.
- [ ] Los cuatro jobs en `"health":"up"`.
- [ ] Loki responde `ready`.
- [ ] Los cuatro servicios del servidor MySQL en `active`.
- [ ] `@@log_slow_extra = 1` y `@@long_query_time = 0.000000`. Si volvieron a `0` y `10.000000`, falta `zz-monitoring.cnf` y la atribución está detenida.

---

## 14. Cómo revertir

### 14.1 Variables de MySQL

```bash
sudo rm /etc/mysql/mysql.conf.d/zz-monitoring.cnf
sudo mysql -e "SET GLOBAL long_query_time = 10; SET GLOBAL log_slow_extra = OFF;"
```

Efecto inmediato, sin reinicio. Use los valores que tenía antes (paso 5).

### 14.2 Servidor MySQL

Cada bloque es independiente. Solo el de `mysqld_exporter` toca MySQL (borra su usuario).

```bash
# slowlog-exporter
sudo systemctl disable --now slowlog-exporter
sudo rm -rf /etc/systemd/system/slowlog-exporter.service /opt/slowlog-exporter /var/lib/slowlog-exporter
sudo userdel slowlog

# Alloy
sudo systemctl disable --now alloy
sudo apt-mark unhold alloy
sudo NEEDRESTART_MODE=l apt-get purge -y alloy
sudo rm -rf /etc/alloy /etc/systemd/system/alloy.service.d /var/lib/alloy
sudo rm -f /etc/apt/sources.list.d/grafana.list /etc/apt/keyrings/grafana.gpg
sudo userdel alloy 2>/dev/null

# mysqld_exporter y su usuario de MySQL
sudo systemctl disable --now mysqld_exporter
sudo rm -rf /etc/systemd/system/mysqld_exporter.service /etc/mysqld_exporter /usr/local/bin/mysqld_exporter
sudo userdel mysqld_exporter
sudo mysql -e "SET sql_log_bin = 0; DROP USER 'exporter'@'localhost'; SET sql_log_bin = 1;"

# node_exporter
sudo systemctl disable --now node_exporter
sudo rm -f /etc/systemd/system/node_exporter.service /usr/local/bin/node_exporter
sudo userdel node_exporter

sudo systemctl daemon-reload
```

### 14.3 AWS

1. EC2 → `<DB_SERVER_NAME>` → **Cambiar grupos de seguridad** → quitar solo `db-metrics-sg`.
2. Borrar el SG `db-metrics-sg`.
3. Terminar la instancia `monitoring-prod` y borrar `monitoring-prod-sg`.

---

## 15. Mejoras pendientes

| Prioridad | Mejora | Motivo |
|---|---|---|
| Alta | Alertas con notificación (Alertmanager + correo, Slack o Teams) | Sin esto nadie se entera si se cae la replicación, se llena el disco o se detiene un exporter. |
| Media | Conectar CloudWatch (rol IAM para `monitoring-prod` + datasource con UID `cloudwatch`; reemplazar `__NLB_ARN_SUFFIX__` en `data-transfer.json`) | Llena los paneles *NLB ProcessedBytes* y *DB EC2 NetworkOut*. |
| Media | Medir el crecimiento real del slow log y ajustar la rotación | El volumen depende de las sentencias por segundo. |
| Baja | Descartar en Alloy las líneas de cabecera del slow log (`/usr/sbin/mysqld`, `Tcp port`, `Time  Id Command`) con otro `stage.drop` | Tras cada rotación llegan a Loki como entradas sueltas. |
| Baja | Restringir la salida de `monitoring-prod-sg` | Por defecto permite todo el tráfico saliente. |
